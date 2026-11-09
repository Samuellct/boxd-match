"""Stage 6: match identified Letterboxd films with local files (no network).

Keys, in order: (tmdb_type, tmdb_id), then the IMDb id. Only tags checked `ok`
by `check-tags` count; a tag not checked yet is matched on its raw id and
reported as unchecked; `suspect` and `not_found` tags never count as present.
File names alone never match: untagged items only get title + year
suggestions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from boxd_match.identity import YEAR_TOLERANCE, normalize

PRESENT = "present"
PARTIAL = "partial"
ABSENT = "absent"

_TYPE_OF_KIND = {"movies": "movie", "shows": "tv"}
_TITLE_YEAR = re.compile(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)")
# Raw release names: "Paris.Texas.1984.MULTi.1080p" (from the former audit.py).
_SCENE = re.compile(r"^(?P<title>.+?)[. _(]+(?P<year>(?:19|20)\d{2})(?:[. _)]|$)")


def guess_title_year(relative_path: str) -> tuple[str, int | None]:
    """Title and year of an untagged file, for suggestions only."""
    parts = relative_path.replace("\\", "/").split("/")
    for part in parts[:-1]:
        m = _TITLE_YEAR.match(part)
        if m:
            return m.group("title").strip(), int(m.group("year"))
    stem = os.path.splitext(parts[-1])[0]
    m = _TITLE_YEAR.match(stem) or _SCENE.match(stem)
    if m:
        return m.group("title").replace(".", " ").replace("_", " ").strip(), int(m.group("year"))
    return stem, None


@dataclass
class LocalItem:
    tag_key: str
    kind: str
    title: str
    year: int | None
    tmdb_key: tuple[str, str] | None
    imdb_id: str | None
    tag_status: str  # ok | suspect | not_found | unchecked
    files: list[sqlite3.Row] = field(default_factory=list)


def _local_items(conn: sqlite3.Connection) -> list[LocalItem]:
    tags = {(r["tag_key"], r["kind"]): r for r in conn.execute("SELECT * FROM local_tag")}
    items: dict[tuple[str, str], LocalItem] = {}
    for f in conn.execute("SELECT * FROM local_file WHERE tag_key IS NOT NULL ORDER BY path"):
        key = (f["tag_key"], f["kind"])
        item = items.get(key)
        if item is None:
            tag = tags.get(key)
            scheme, value = f["tag_key"].split(":", 1)
            if tag is not None:
                tmdb_key = (tag["tmdb_type"], tag["tmdb_id"]) if tag["tmdb_id"] else None
                imdb_id, status = tag["imdb_id"], tag["status"]
            else:
                tmdb_key = (_TYPE_OF_KIND[f["kind"]], value) if scheme == "tmdb" else None
                imdb_id, status = (value if scheme == "imdb" else None), "unchecked"
            item = items[key] = LocalItem(f["tag_key"], f["kind"], f["title"], f["year"], tmdb_key, imdb_id, status)
        item.files.append(f)
    return list(items.values())


def _tv_details(conn: sqlite3.Connection, tmdb_id: str) -> dict:
    row = conn.execute("SELECT body FROM tmdb_cache WHERE key = ?", (f"tv/{tmdb_id}",)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def _episodes(files: list[sqlite3.Row], season: int | None = None) -> set[tuple[int, int]]:
    return {
        (f["season"], f["episode"]) for f in files
        if f["season"] is not None and f["episode"] is not None and f["season"] > 0
        and (season is None or f["season"] == season)
    }


def _state(identity: sqlite3.Row, files: list[sqlite3.Row], conn: sqlite3.Connection):
    """(state, files kept, episodes_local, episodes_total) for one Letterboxd identity."""
    if identity["tmdb_type"] != "tv":
        return (PRESENT if files else ABSENT), files, None, None
    season, episode = identity["tmdb_season"], identity["tmdb_episode"]
    if episode is not None:
        kept = [f for f in files if f["season"] == season and f["episode"] == episode]
        return (PRESENT if kept else ABSENT), kept, None, None
    details = _tv_details(conn, identity["tmdb_id"])
    if season is not None:
        kept = [f for f in files if f["season"] == season]
        total = next((s.get("episode_count") for s in details.get("seasons", []) if s.get("season_number") == season), None)
        have = len({(f["season"], f["episode"]) for f in kept if f["episode"] is not None})
    else:
        kept = files
        total = details.get("number_of_episodes")
        have = len(_episodes(kept))
    if not kept:
        return ABSENT, kept, have, total
    complete = total is None or have >= total
    return (PRESENT if complete else PARTIAL), kept, have, total


@dataclass
class MatchResult:
    states: dict[str, int]
    unchecked_used: int
    local_only: list[LocalItem]
    flagged_tags: list[LocalItem]
    suggestions: list[dict]


def match(conn: sqlite3.Connection) -> MatchResult:
    items = _local_items(conn)
    by_tmdb: dict[tuple[str, str], list[LocalItem]] = defaultdict(list)
    by_imdb: dict[str, list[LocalItem]] = defaultdict(list)
    for item in items:
        if item.tag_status not in ("ok", "unchecked"):
            continue
        if item.tmdb_key:
            by_tmdb[item.tmdb_key].append(item)
        if item.imdb_id:
            by_imdb[item.imdb_id].append(item)

    identities = conn.execute(
        "SELECT i.* FROM identity i JOIN film f ON f.uri = i.uri WHERE i.status = 'confirmed' AND i.tmdb_id IS NOT NULL"
    ).fetchall()
    used: set[int] = set()
    rows, states, unchecked_used = [], defaultdict(int), 0
    for identity in identities:
        found = by_tmdb.get((identity["tmdb_type"], identity["tmdb_id"]), [])
        method = "tmdb" if found else None
        if not found and identity["imdb_id"]:
            found = [i for i in by_imdb.get(identity["imdb_id"], []) if _TYPE_OF_KIND[i.kind] == identity["tmdb_type"]]
            method = "imdb" if found else None
        files = [f for item in found for f in item.files]
        state, kept, have, total = _state(identity, files, conn)
        if state != ABSENT:
            used.update(id(item) for item in found)
            unchecked_used += sum(item.tag_status == "unchecked" for item in found)
        states[state] += 1
        rows.append((
            identity["uri"], state, method if state != ABSENT else None,
            json.dumps(sorted({i.tag_key for i in found})) if state != ABSENT else "[]",
            json.dumps([{"path": f["path"], "available": bool(f["available"])} for f in kept], ensure_ascii=False),
            have, total, json.dumps(sorted({f["res"] for f in kept if f["res"]})),
        ))
    with conn:
        conn.execute("DELETE FROM local_match")
        conn.executemany("INSERT INTO local_match VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    local_only = [i for i in items if i.tag_status in ("ok", "unchecked") and id(i) not in used]
    flagged = [i for i in items if i.tag_status in ("suspect", "not_found")]
    return MatchResult(dict(states), unchecked_used, local_only, flagged, _suggestions(conn))


def _suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Untagged local items -> Letterboxd films with the same title and a close year. Never a match."""
    films = conn.execute("SELECT uri, name, year FROM film").fetchall()
    by_title: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for film in films:
        by_title[normalize(film["name"])].append(film)
    roots = {r["id"]: r["path"] for r in conn.execute("SELECT id, path FROM local_root")}
    out = []
    for item in conn.execute(
        "SELECT root, item, MIN(path) AS path FROM local_file WHERE tag_key IS NULL"
        " GROUP BY root, item ORDER BY root, item"
    ):
        relative = os.path.relpath(item["path"], roots[item["root"]]) if item["root"] in roots else item["item"]
        title, year = guess_title_year(relative)
        candidates = [
            f["uri"] for f in by_title.get(normalize(title), [])
            if year is None or (f["year"].isdigit() and abs(int(f["year"]) - year) <= YEAR_TOLERANCE)
        ]
        out.append({"root": item["root"], "item": item["item"], "title": title, "year": year,
                    "candidates": candidates})
    return out


def status_breakdown(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Local state per Letterboxd status: watched, to watch, owned list role, all."""
    groups = {
        "all": "1",
        "watched": "f.watched",
        "to_watch": "NOT f.watched AND (f.on_watchlist OR EXISTS"
                    " (SELECT 1 FROM json_each(f.roles) WHERE value = 'watchlist'))",
        "owned_role": "EXISTS (SELECT 1 FROM json_each(f.roles) WHERE value = 'owned')",
    }
    out = {}
    for name, condition in groups.items():
        out[name] = {
            r[0]: r[1] for r in conn.execute(
                f"SELECT m.state, COUNT(*) FROM local_match m JOIN film f ON f.uri = m.uri WHERE {condition}"
                " GROUP BY m.state"
            )
        }
    return out
