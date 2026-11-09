"""Check each local tag against TMDB.

A tag is the external id written in a file or folder name. It is `ok` when
TMDB agrees with the title and year of that name (title, original title or a
translation; year within identity.YEAR_TOLERANCE). Here the file name is the
second source, so `/find` only maps an IMDb id to TMDB and never confirms by
itself.

    ok         -> tag and name agree (or accepted in the review file)
    suspect    -> the tag points to another title, year or type
    not_found  -> TMDB does not know the id
    ignored    -> excluded in the review file
Manual decisions live in `tag_decision` and are reapplied after every check.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from boxd_match import identity as idt
from boxd_match.db import CONFIRMED, now
from boxd_match.tmdb import TmdbClient

OK = "ok"
SUSPECT = "suspect"
NOT_FOUND = "not_found"
IGNORED = "ignored"
MANUAL = "manual"

_TYPE_OF_KIND = {"movies": "movie", "shows": "tv"}


@dataclass
class TagCheck:
    done: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str) -> None:
        self.done += 1
        self.counts[key] = self.counts.get(key, 0) + 1


def _target(tmdb: TmdbClient, tag_key: str, kind: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(tmdb_type, tmdb_id, imdb_id, reason) for a tag; reason is set when no target exists."""
    scheme, value = tag_key.split(":", 1)
    wanted = _TYPE_OF_KIND[kind]
    if scheme == "tmdb":
        return (wanted, value, None, None) if value.isdigit() else (None, None, None, idt.TMDB_NOT_FOUND)
    status, body = tmdb.find(value)
    hits = idt.find_hits(body) if status == 200 else set()
    same_type = sorted(h for h in hits if h[0] == wanted)
    if same_type:
        return same_type[0][0], same_type[0][1], value, None
    if hits:
        other = sorted(hits)[0]
        return other[0], other[1], value, idt.TYPE_MISMATCH
    return None, None, value, idt.TMDB_NOT_FOUND


def check_tags(
    conn: sqlite3.Connection, tmdb: TmdbClient, report: Callable[[str], None] = lambda s: None,
    recheck: bool = False,
) -> TagCheck:
    """Check every (tag, kind) of the scan that has no result yet (all of them with recheck)."""
    progress = TagCheck()
    if recheck:
        conn.execute("DELETE FROM local_tag")
        conn.commit()
    rows = conn.execute(
        "SELECT f.tag_key, f.kind, MIN(f.title) AS title, MIN(f.year) AS year FROM local_file f"
        " LEFT JOIN local_tag t ON t.tag_key = f.tag_key AND t.kind = f.kind"
        " WHERE f.tag_key IS NOT NULL AND t.tag_key IS NULL GROUP BY f.tag_key, f.kind ORDER BY f.tag_key"
    ).fetchall()
    for n, row in enumerate(rows, 1):
        tmdb_type, tmdb_id, imdb_id, reason = _target(tmdb, row["tag_key"], row["kind"])
        status = NOT_FOUND if reason == idt.TMDB_NOT_FOUND else SUSPECT if reason else None
        if status is None:
            details_status, details = tmdb.details(tmdb_type, tmdb_id)
            verdict = idt.check_candidate(
                name=row["title"], year=str(row["year"] or ""), tmdb_type=tmdb_type, tmdb_id=tmdb_id,
                details_status=details_status, details=details, find_body=None, imdb_id=None,
                require_find=False,
            )
            imdb_id = imdb_id or verdict.imdb_id
            if verdict.status == CONFIRMED:
                status = OK
            elif verdict.reason == idt.TMDB_NOT_FOUND:
                status, reason = NOT_FOUND, verdict.reason
            else:
                status, reason = SUSPECT, verdict.reason
        conn.execute(
            "INSERT OR REPLACE INTO local_tag VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["tag_key"], row["kind"], tmdb_type, tmdb_id, imdb_id, status, reason,
             row["title"], row["year"], now()),
        )
        conn.commit()
        progress.add(status + (f":{reason}" if reason else ""))
        if n % 100 == 0:
            report(f"tags {n}/{len(rows)}")
    apply_tag_decisions(conn)
    return progress


def apply_tag_decisions(conn: sqlite3.Connection) -> int:
    """Reapply the review decisions: "ok", "ignore", or another target "movie/ID" / "tv/ID"."""
    applied = 0
    with conn:
        for d in conn.execute("SELECT * FROM tag_decision").fetchall():
            where = (d["tag_key"], d["kind"])
            if d["decision"] in ("ok", "ignore"):
                status = OK if d["decision"] == "ok" else IGNORED
                cursor = conn.execute(
                    "UPDATE local_tag SET status = ?, reason = ? WHERE tag_key = ? AND kind = ?",
                    (status, MANUAL, *where))
            else:
                tmdb_type, tmdb_id = d["decision"].split("/")
                cursor = conn.execute(
                    "UPDATE local_tag SET status = ?, reason = ?, tmdb_type = ?, tmdb_id = ?"
                    " WHERE tag_key = ? AND kind = ?", (OK, MANUAL, tmdb_type, tmdb_id, *where))
            applied += cursor.rowcount
    return applied
