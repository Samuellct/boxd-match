"""Stages 3 and 4: give each film a TMDB identity and check it.

Order: legacy cache entries go through TMDB first; the Letterboxd page is read
only for films the cache cannot confirm. Every film is committed as soon as it
is processed, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from boxd_match import identity as idt
from boxd_match.db import CONFIRMED, NEEDS_PAGE, PENDING, TO_REVIEW, UNRESOLVED, now
from boxd_match.letterboxd import FilmPage, is_challenge, parse_film_page, slug_from_url
from boxd_match.net import NetworkError, Transport
from boxd_match.tmdb import TmdbClient

LEGACY = "legacy"
PAGE = "page"
MANUAL = "manual"
MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_RETRY_AFTER = 30.0


class ResolveError(Exception):
    """A run stopped on purpose (bad key, network down): progress is saved."""


class Blocked(ResolveError):
    """Letterboxd refused or challenged us. Stop, never work around it."""


@dataclass
class SeedResult:
    seeded: int = 0
    not_in_export: int = 0
    kept: int = 0  # film already has a non-legacy or checked identity
    errors: int = 0


def seed_legacy(conn: sqlite3.Connection, cache_file: str | Path) -> SeedResult:
    """Import `letterboxd_ids.json` as unchecked candidates. Never confirms anything."""
    data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
    result = SeedResult()
    with conn:
        for uri, entry in data.items():
            if not isinstance(entry, dict) or entry.get("error"):
                result.errors += 1
                continue
            row = conn.execute("SELECT source, status FROM identity WHERE uri = ?", (uri,)).fetchone()
            if row is None:
                result.not_in_export += 1
                continue
            if row["status"] != PENDING or row["source"] not in (None, LEGACY):
                result.kept += 1
                continue
            conn.execute(
                "UPDATE identity SET source = ?, tmdb_type = NULL, tmdb_id = ?, imdb_id = ?, slug = ?,"
                " reason = NULL WHERE uri = ?",
                (LEGACY, entry.get("tmdb") or None, entry.get("imdb") or None, slug_from_url(entry.get("page")), uri),
            )
            result.seeded += 1
    return result


@dataclass
class Progress:
    done: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str) -> None:
        self.done += 1
        self.counts[key] = self.counts.get(key, 0) + 1


def _film(conn: sqlite3.Connection, uri: str) -> sqlite3.Row:
    return conn.execute("SELECT name, year FROM film WHERE uri = ?", (uri,)).fetchone()


def _save(conn: sqlite3.Connection, uri: str, **values) -> None:
    values["checked_at"] = now()
    assignments = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE identity SET {assignments} WHERE uri = ?", (*values.values(), uri))
    conn.commit()


def check_legacy(
    conn: sqlite3.Connection, tmdb: TmdbClient, report: Callable[[str], None] = lambda s: None
) -> Progress:
    """TMDB check of every unchecked legacy candidate.

    The cache has no type: a candidate is confirmed only if `/find` on its IMDb
    id returns the same movie id. Anything else goes to `needs_page`.
    """
    progress = Progress()
    rows = conn.execute(
        "SELECT i.uri, i.tmdb_id, i.imdb_id FROM identity i JOIN film f ON f.uri = i.uri"
        " WHERE i.source = ? AND i.status = ? ORDER BY i.uri",
        (LEGACY, PENDING),
    ).fetchall()
    for n, row in enumerate(rows, 1):
        uri = row["uri"]
        if not row["tmdb_id"]:
            _save(conn, uri, status=NEEDS_PAGE, reason=idt.NO_TMDB_ID)
            progress.add(NEEDS_PAGE + ":" + idt.NO_TMDB_ID)
            continue
        film = _film(conn, uri)
        status, details = tmdb.details("movie", row["tmdb_id"])
        find_body = tmdb.find(row["imdb_id"])[1] if row["imdb_id"] else None
        verdict = idt.check_candidate(
            name=film["name"], year=film["year"], tmdb_type="movie", tmdb_id=row["tmdb_id"],
            details_status=status, details=details, find_body=find_body,
            imdb_id=row["imdb_id"], require_find=True,
        )
        if verdict.status == CONFIRMED:
            _save(conn, uri, status=CONFIRMED, tmdb_type="movie", method=verdict.method,
                  imdb_id=verdict.imdb_id, reason=None)
            progress.add(CONFIRMED)
        else:
            _save(conn, uri, status=NEEDS_PAGE, reason=verdict.reason)
            progress.add(NEEDS_PAGE + ":" + verdict.reason)
        if n % 100 == 0:
            report(f"legacy {n}/{len(rows)}")
    return progress


class PageFetcher:
    """Letterboxd page reads with a permanent cache and stop-on-block rules.

    Block = HTTP 403 or 503, a challenge page, two 429 in a row, or
    MAX_CONSECUTIVE_FAILURES failures in a row (network errors, 5xx).
    """

    def __init__(self, conn: sqlite3.Connection, transport: Transport,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._conn = conn
        self._transport = transport
        self._sleep = sleep
        self._failures = 0
        self.requests = 0

    def cached(self, uri: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM page_cache WHERE uri = ?", (uri,)).fetchone()

    def _store(self, uri: str, status: int, page: FilmPage | None, final_url: str | None) -> sqlite3.Row:
        self._conn.execute(
            "INSERT OR REPLACE INTO page_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uri, status, final_url, page and page.link_type, page and page.link_id,
             page and page.attr_id, page and page.imdb_id, now()),
        )
        self._conn.commit()
        return self.cached(uri)

    def _failed(self, what: str) -> None:
        self._failures += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            raise Blocked(f"{self._failures} failures in a row on Letterboxd (last: {what})")

    def get(self, uri: str) -> sqlite3.Row | None:
        """Cached page row, or None when this film must be retried on a later run."""
        row = self.cached(uri)
        if row is not None:
            return row
        throttled = False
        while True:
            try:
                response = self._transport.get(uri)
            except NetworkError as error:
                self._failed(str(error))
                return None
            self.requests += 1
            if response.status == 429 and not throttled:
                throttled = True
                self._sleep(float(response.headers.get("Retry-After") or DEFAULT_RETRY_AFTER))
                continue
            break
        if response.status in (403, 429, 503) or is_challenge(response):
            what = "a challenge page" if is_challenge(response) else f"HTTP {response.status}"
            raise Blocked(f"Letterboxd answered {what} for {uri}")
        if response.status == 404:
            self._failures = 0
            return self._store(uri, 404, None, response.url)
        if response.status != 200:
            self._failed(f"HTTP {response.status} for {uri}")
            return None
        self._failures = 0
        return self._store(uri, 200, parse_film_page(response.url, response.text()), response.url)


def _page_verdict(film: sqlite3.Row, page: sqlite3.Row, tmdb: TmdbClient) -> dict:
    slug = slug_from_url(page["final_url"])
    base = {"source": PAGE, "slug": slug, "imdb_id": page["imdb_id"], "method": None}
    if page["http_status"] == 404:
        return {**base, "status": UNRESOLVED, "reason": idt.PAGE_NOT_FOUND, "tmdb_type": None, "tmdb_id": None}
    if page["link_id"]:
        tmdb_type, tmdb_id, require_find = page["link_type"], page["link_id"], False
    elif page["attr_id"]:
        # No link: the attribute alone is not trusted for the type, /find must agree.
        tmdb_type, tmdb_id, require_find = "movie", page["attr_id"], True
    else:
        return {**base, "status": UNRESOLVED, "reason": idt.NO_TMDB_LINK, "tmdb_type": None, "tmdb_id": None}
    status, details = tmdb.details(tmdb_type, tmdb_id)
    find_body = tmdb.find(page["imdb_id"])[1] if page["imdb_id"] else None
    verdict = idt.check_candidate(
        name=film["name"], year=film["year"], tmdb_type=tmdb_type, tmdb_id=tmdb_id,
        details_status=status, details=details, find_body=find_body,
        imdb_id=page["imdb_id"], require_find=require_find,
    )
    result, reason = verdict.status, verdict.reason
    if page["link_id"] and page["attr_id"] and page["link_id"] != page["attr_id"]:
        result, reason = TO_REVIEW, idt.LINK_ATTR_CONFLICT
    elif not page["link_id"] and result != CONFIRMED:
        reason = idt.NO_TMDB_LINK
    return {**base, "status": result, "reason": reason,
            "method": verdict.method if result == CONFIRMED else None,
            "tmdb_type": tmdb_type, "tmdb_id": tmdb_id, "imdb_id": verdict.imdb_id or page["imdb_id"]}


def read_pages(
    conn: sqlite3.Connection, fetcher: PageFetcher, tmdb: TmdbClient,
    report: Callable[[str], None] = lambda s: None,
) -> Progress:
    """Read the page of every film not settled by the legacy cache, then check it on TMDB."""
    progress = Progress()
    rows = conn.execute(
        "SELECT i.uri, f.name, f.year FROM identity i JOIN film f ON f.uri = i.uri"
        " WHERE i.status IN (?, ?) ORDER BY i.status DESC, i.uri",
        (PENDING, NEEDS_PAGE),
    ).fetchall()
    for n, film in enumerate(rows, 1):
        page = fetcher.get(film["uri"])
        if page is None:
            progress.add("retry_later")
            continue
        values = _page_verdict(film, page, tmdb)
        _save(conn, film["uri"], **values)
        progress.add(values["status"] + (":" + values["reason"] if values["reason"] else ""))
        if n % 25 == 0:
            report(f"pages {n}/{len(rows)} (letterboxd requests {fetcher.requests})")
    return progress


def attach_redirects(conn: sqlite3.Connection, fetcher: PageFetcher | None = None) -> int:
    """Attach `pending_redirect` diary entries through their redirect slug.

    With no fetcher, only cached redirects are used (import time, no network).
    """
    attached = 0
    rows = conn.execute("SELECT DISTINCT entry_uri FROM entry_link WHERE status = 'pending_redirect'").fetchall()
    for row in rows:
        entry_uri = row["entry_uri"]
        if fetcher is not None:
            page = fetcher.get(entry_uri)
        else:
            page = conn.execute("SELECT * FROM page_cache WHERE uri = ?", (entry_uri,)).fetchone()
        slug = slug_from_url(page["final_url"]) if page is not None else None
        if not slug:
            continue
        films = conn.execute(
            "SELECT i.uri FROM identity i JOIN film f ON f.uri = i.uri WHERE i.slug = ?", (slug,)
        ).fetchall()
        if len(films) != 1:
            continue
        with conn:
            conn.execute("UPDATE entry_link SET status = 'attached', film_uri = ? WHERE entry_uri = ?",
                         (films[0]["uri"], entry_uri))
            conn.execute("UPDATE viewing SET film_uri = ? WHERE entry_uri = ?", (films[0]["uri"], entry_uri))
        attached += 1
    return attached


def non_final(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM identity i JOIN film f ON f.uri = i.uri WHERE i.status IN (?, ?)",
        (PENDING, NEEDS_PAGE),
    ).fetchone()[0]


def stderr_report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)

