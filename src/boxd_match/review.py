"""Manual review through a TOML file.

`export_review` writes one `[[film]]` block per film that is `to_review` or
`unresolved` (manual decisions excluded). The user fills `decision`, then
`apply_review` records it:
    "movie/ID" or "tv/ID"  -> confirmed, method `manual`
    "tv/ID/season/N"       -> one season of a series (Twin Peaks: The Return)
    "tv/ID/season/N/episode/M" -> one episode; season 0 holds the specials
    "none"                 -> unresolved, reason `manual_none` (not on TMDB)
    ""                     -> skipped, decide later
The whole file is validated before anything is written.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path

from boxd_match.db import CONFIRMED, TO_REVIEW, UNRESOLVED, now
from boxd_match.identity import BY_MANUAL
from boxd_match.resolver import MANUAL

_DECISION = re.compile(r"^(movie|tv)/([1-9]\d*)(?:/season/(\d+)(?:/episode/([1-9]\d*))?)?$")
MANUAL_NONE = "manual_none"

_HEADER = """\
# boxd-match review file. Fill `decision` for each film, then run:
#   boxd-match review apply {name}
# decision = "movie/ID" or "tv/ID"  -> confirmed with this TMDB id
# decision = "tv/ID/season/N"       -> one season of the series
# decision = "tv/ID/season/N/episode/M" -> one episode (season 0 = specials)
# decision = "none"                 -> not on TMDB
# decision = ""                     -> skipped, decide later
# Applying is safe to repeat and accepts a partly filled file.

"""


class ReviewError(Exception):
    pass


def _toml(value: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def _describe(tmdb_type: str, body: dict) -> str:
    title = body.get("name") or body.get("title") or "?"
    date = body.get("first_air_date") or body.get("release_date") or ""
    return f"{tmdb_type}/{body['id']}  {title} ({date[:4] or '?'})  https://www.themoviedb.org/{tmdb_type}/{body['id']}"


def _candidates(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    found: dict[str, str] = {}
    if row["tmdb_type"] and row["tmdb_id"]:
        cached = conn.execute("SELECT body FROM tmdb_cache WHERE key = ?",
                              (f"{row['tmdb_type']}/{row['tmdb_id']}",)).fetchone()
        key = f"{row['tmdb_type']}/{row['tmdb_id']}"
        found[key] = _describe(row["tmdb_type"], json.loads(cached[0])) if cached and cached[0] else key
    if row["imdb_id"]:
        cached = conn.execute("SELECT body FROM find_cache WHERE imdb_id = ?", (row["imdb_id"],)).fetchone()
        body = json.loads(cached[0]) if cached and cached[0] else {}
        for tmdb_type, results in (("movie", body.get("movie_results", [])), ("tv", body.get("tv_results", []))):
            for result in results:
                found.setdefault(f"{tmdb_type}/{result['id']}", _describe(tmdb_type, result))
    return list(found.values())


@dataclass
class ExportResult:
    path: Path
    films: int


def export_review(conn: sqlite3.Connection, path: str | Path, force: bool = False) -> ExportResult:
    path = Path(path)
    if path.exists() and not force:
        raise ReviewError(f"{path} already exists (it may hold decisions): apply it first, or pass --force")
    rows = conn.execute(
        "SELECT i.*, f.name, f.year FROM identity i JOIN film f ON f.uri = i.uri"
        " WHERE i.status IN (?, ?) AND COALESCE(i.source, '') != ? ORDER BY i.status, i.reason, f.name",
        (TO_REVIEW, UNRESOLVED, MANUAL),
    ).fetchall()
    blocks = [_HEADER.format(name=path.name)]
    for row in rows:
        page = f"https://letterboxd.com/film/{row['slug']}/" if row["slug"] else row["uri"]
        candidates = _candidates(conn, row)
        title = "{} ({})".format(row["name"], row["year"])
        blocks.append("\n".join([
            "[[film]]",
            f"uri = {_toml(row['uri'])}",
            f"title = {_toml(title)}",
            f"status = {_toml(row['status'])}",
            f"reason = {_toml(row['reason'] or '')}",
            f"letterboxd = {_toml(page)}",
            f"imdb = {_toml(row['imdb_id'] or '')}",
            "candidates = [" + "".join(f"\n  {_toml(c)}," for c in candidates) + ("\n]" if candidates else "]"),
            'decision = ""',
            "",
        ]))
    path.write_text("\n".join(blocks), encoding="utf-8")
    return ExportResult(path, len(rows))


def _parse(decision: str) -> tuple[str, str, int | None, int | None] | None:
    """`tv/1920/season/3` -> ("tv", "1920", 3, None). Seasons and episodes are TV only."""
    match = _DECISION.match(decision)
    if match is None:
        return None
    tmdb_type, tmdb_id, season, episode = match.groups()
    if tmdb_type == "movie" and season is not None:
        return None
    return tmdb_type, tmdb_id, (int(season) if season is not None else None), (int(episode) if episode else None)


@dataclass
class ApplyResult:
    applied: int
    skipped: int


def apply_review(conn: sqlite3.Connection, path: str | Path) -> ApplyResult:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReviewError(f"{path}: {error}") from error
    blocks = data.get("film", [])
    errors: list[str] = []
    decisions: list[tuple[str, str]] = []
    for n, block in enumerate(blocks, 1):
        uri = str(block.get("uri", ""))
        decision = str(block.get("decision", "")).strip()
        if not decision:
            continue
        if conn.execute("SELECT 1 FROM identity WHERE uri = ?", (uri,)).fetchone() is None:
            errors.append(f"block {n}: unknown uri {uri!r}")
        elif decision != "none" and not _parse(decision):
            errors.append(
                f"block {n} ({uri}): decision {decision!r} is not movie/ID, tv/ID,"
                " tv/ID/season/N, tv/ID/season/N/episode/M or none"
            )
        else:
            decisions.append((uri, decision))
    if errors:
        raise ReviewError("nothing applied:\n  " + "\n  ".join(errors))
    with conn:
        for uri, decision in decisions:
            conn.execute("INSERT OR REPLACE INTO review_decision VALUES (?, ?, ?)", (uri, decision, now()))
            if decision == "none":
                conn.execute(
                    "UPDATE identity SET source = ?, status = ?, reason = ?, method = NULL, tmdb_type = NULL,"
                    " tmdb_id = NULL, tmdb_season = NULL, tmdb_episode = NULL, checked_at = ? WHERE uri = ?",
                    (MANUAL, UNRESOLVED, MANUAL_NONE, now(), uri),
                )
            else:
                tmdb_type, tmdb_id, season, episode = _parse(decision)
                conn.execute(
                    "UPDATE identity SET source = ?, status = ?, reason = NULL, method = ?, tmdb_type = ?,"
                    " tmdb_id = ?, tmdb_season = ?, tmdb_episode = ?, checked_at = ? WHERE uri = ?",
                    (MANUAL, CONFIRMED, BY_MANUAL, tmdb_type, tmdb_id, season, episode, now(), uri),
                )
    return ApplyResult(applied=len(decisions), skipped=len(blocks) - len(decisions))
