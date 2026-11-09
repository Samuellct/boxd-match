"""Read-only scan of the local library roots.

`parse()` is copied from suivi-medias/app/inventory.py, which reads the
owner's Jellyfin-style naming with zero anomalies on the real library:

    <root>/<Title> (<Year>) [imdbid-tt123]/<Title> (<Year>) [imdbid-tt123] - 2160p - FR.mkv
    <root>/<Show> (<Year>) [tmdbid-1920]/Season 03/<Show> S03E01.mkv

Nothing here writes, moves or deletes media files.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

from boxd_match.config import Root
from boxd_match.db import now

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".iso"}

RE_TAG = re.compile(r"\[(imdbid|tmdbid)-([^\]]+)\]", re.I)
RE_TITLE_YEAR = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)")
RE_SEASON = re.compile(r"^season\s*(\d+)$", re.I)
# Up to 4 digits: absolute anime numbering (One Piece S01E1086).
RE_EPISODE = re.compile(r"S(\d{1,2})E(\d{1,4})", re.I)
RE_RES = re.compile(r"^(2160p|1080p|720p|480p)$", re.I)
RE_FLAG = re.compile(r"^[A-Z0-9]{3,5}$")


def parse(root: Root, path: str, size: int) -> dict:
    """One video file -> its library item (the tagged folder or file) and details."""
    rel = os.path.relpath(path, root.path)
    parts = rel.split(os.sep)
    tagged = [i for i, c in enumerate(parts) if RE_TAG.search(c)]
    item_idx = tagged[0] if tagged else len(parts) - 1
    item_name = parts[item_idx]
    is_file_item = item_idx == len(parts) - 1
    item_stem = os.path.splitext(item_name)[0] if is_file_item else item_name
    item_n = item_stem.replace("\xa0", " ")
    file_stem = os.path.splitext(parts[-1])[0]
    file_n = file_stem.replace("\xa0", " ")

    tag = RE_TAG.search(item_n)
    file_tag = RE_TAG.search(file_n)
    ty = RE_TITLE_YEAR.search(item_n)

    tokens = []
    if file_tag:
        suffix = file_n[file_tag.end():].strip().lstrip("-").strip()
        tokens = [t.strip() for t in suffix.split(" - ") if t.strip()]
    name_res = next((t.lower() for t in tokens if RE_RES.match(t)), None)
    flags, rest = [], []
    for t in tokens:
        if RE_RES.match(t):
            continue
        words = t.split()
        if words and all(RE_FLAG.match(w) and not RE_RES.match(w) for w in words):
            flags += words
        else:
            rest.append(t)

    folder_season = None
    for c in parts[item_idx + 1:-1]:
        m = RE_SEASON.match(c.strip())
        if m:
            folder_season = int(m.group(1))
    ep = RE_EPISODE.search(file_n)
    season = int(ep.group(1)) if ep else folder_season

    return {
        "root": root.id, "kind": root.kind, "path": path, "bytes": size,
        "item": item_name,
        "tag_key": f"{tag.group(1).lower()[:4]}:{tag.group(2).lower()}" if tag else None,
        "title": ty.group("title").strip() if ty else item_n,
        "year": int(ty.group("year")) if ty else None,
        "res": name_res or root.resolution,
        "edition": " / ".join(rest), "flags": " ".join(flags),
        "season": season, "episode": int(ep.group(2)) if ep else None,
    }


def scan_root(root: Root) -> list[dict] | None:
    """Parsed video files of one root, or None when the root folder is absent."""
    if not os.path.isdir(root.path):
        return None
    files = []
    for dirpath, _, filenames in os.walk(root.path):
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in VIDEO:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            record = parse(root, path, st.st_size)
            record["mtime"] = int(st.st_mtime)
            files.append(record)
    return files


@dataclass
class RootStatus:
    id: str
    path: str
    present: bool
    optional: bool
    files: int


_COLUMNS = ("path", "root", "kind", "item", "tag_key", "title", "year", "season", "episode",
            "res", "edition", "flags", "bytes", "mtime")


def scan(conn: sqlite3.Connection, roots: tuple[Root, ...]) -> list[RootStatus]:
    """Rescan every root. An absent root keeps its last known files, flagged unavailable."""
    statuses = []
    configured = {r.id for r in roots}
    with conn:
        # Roots removed from the configuration leave the database.
        for (root_id,) in conn.execute("SELECT id FROM local_root").fetchall():
            if root_id not in configured:
                conn.execute("DELETE FROM local_file WHERE root = ?", (root_id,))
                conn.execute("DELETE FROM local_root WHERE id = ?", (root_id,))
        for root in roots:
            found = scan_root(root)
            if found is None:
                conn.execute("UPDATE local_file SET available = 0 WHERE root = ?", (root.id,))
                kept = conn.execute("SELECT COUNT(*) FROM local_file WHERE root = ?", (root.id,)).fetchone()[0]
                statuses.append(RootStatus(root.id, root.path, False, root.optional, kept))
            else:
                conn.execute("DELETE FROM local_file WHERE root = ?", (root.id,))
                conn.executemany(
                    f"INSERT INTO local_file ({', '.join(_COLUMNS)}, available)"
                    f" VALUES ({', '.join('?' * len(_COLUMNS))}, 1)",
                    [tuple(f[c] for c in _COLUMNS) for f in found],
                )
                statuses.append(RootStatus(root.id, root.path, True, root.optional, len(found)))
            conn.execute(
                "INSERT OR REPLACE INTO local_root VALUES (?, ?, ?, ?, ?, ?)",
                (root.id, root.path, root.kind, int(root.optional), int(found is not None), now()),
            )
    return statuses
