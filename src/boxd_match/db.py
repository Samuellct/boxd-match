"""SQLite store.

Two kinds of tables:
- export tables (`film`, `viewing`, `entry_link`, `list`, `list_entry`, `orphan`)
  are rebuilt from scratch on every import;
- permanent tables (`identity`, `page_cache`, `tmdb_cache`, `find_cache`,
  `review_decision`) survive re-imports: they hold network results and the
  user's decisions.

The e-mail of `profile.csv` never reaches this module: the export reader does
not open that file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from boxd_match.films import ATTACHED, Library

SCHEMA_VERSION = 1

# Identity statuses. `pending` = not checked yet; `needs_page` = the legacy
# cache candidate failed the TMDB check, so the Letterboxd page must be read.
PENDING = "pending"
NEEDS_PAGE = "needs_page"
CONFIRMED = "confirmed"
TO_REVIEW = "to_review"
UNRESOLVED = "unresolved"
FINAL_STATUSES = (CONFIRMED, TO_REVIEW, UNRESOLVED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS film (
    uri TEXT PRIMARY KEY, name TEXT NOT NULL, year TEXT NOT NULL,
    watched INTEGER NOT NULL, on_watchlist INTEGER NOT NULL, liked INTEGER NOT NULL,
    rating REAL, roles TEXT NOT NULL  -- JSON list of configured list roles
);
CREATE TABLE IF NOT EXISTS viewing (
    entry_uri TEXT PRIMARY KEY, film_uri TEXT, logged TEXT, watched_on TEXT,
    rating REAL, rewatch INTEGER NOT NULL, tags TEXT NOT NULL, review TEXT
);
CREATE TABLE IF NOT EXISTS entry_link (
    source TEXT NOT NULL, entry_uri TEXT NOT NULL, name TEXT NOT NULL, year TEXT NOT NULL,
    status TEXT NOT NULL, film_uri TEXT, candidates TEXT NOT NULL,
    PRIMARY KEY (source, entry_uri)
);
CREATE TABLE IF NOT EXISTS list (
    slug TEXT PRIMARY KEY, name TEXT NOT NULL, date TEXT, tags TEXT NOT NULL,
    url TEXT, description TEXT, roles TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_entry (
    slug TEXT NOT NULL, position INTEGER NOT NULL, film_uri TEXT NOT NULL, note TEXT,
    PRIMARY KEY (slug, position)
);
CREATE TABLE IF NOT EXISTS orphan (
    source TEXT NOT NULL, name TEXT NOT NULL, year TEXT NOT NULL, suggestions TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity (
    uri TEXT PRIMARY KEY,
    source TEXT,          -- legacy | page | manual
    tmdb_type TEXT,       -- movie | tv | NULL (legacy: unknown)
    tmdb_id TEXT,
    imdb_id TEXT,
    slug TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    method TEXT,          -- how a confirmed identity was confirmed
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS page_cache (
    uri TEXT PRIMARY KEY, http_status INTEGER NOT NULL, final_url TEXT,
    link_type TEXT, link_id TEXT, attr_id TEXT, imdb_id TEXT, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tmdb_cache (
    key TEXT PRIMARY KEY, http_status INTEGER NOT NULL, body TEXT, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS find_cache (
    imdb_id TEXT PRIMARY KEY, http_status INTEGER NOT NULL, body TEXT, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_decision (
    uri TEXT PRIMARY KEY, decision TEXT NOT NULL, decided_at TEXT NOT NULL
);
"""

_EXPORT_TABLES = ("film", "viewing", "entry_link", "list", "list_entry", "orphan")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SchemaError(Exception):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row["value"]) != SCHEMA_VERSION:
        raise SchemaError(f"{path}: schema version {row['value']}, expected {SCHEMA_VERSION}")
    return conn


def import_library(conn: sqlite3.Connection, library: Library, source: str) -> None:
    """Replace the export tables; leave identities, caches and decisions alone."""
    with conn:
        for table in _EXPORT_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT INTO film VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (f.uri, f.name, f.year, f.watched, f.on_watchlist, f.liked, f.rating, json.dumps(sorted(f.roles)))
                for f in library.films.values()
            ],
        )
        film_of_entry = {
            link.entry_uri: link.film_uri for link in library.entry_links if link.status == ATTACHED
        }
        conn.executemany(
            "INSERT INTO viewing VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (v.entry_uri, film_of_entry.get(v.entry_uri), v.logged, v.watched_on, v.rating,
                 v.rewatch, json.dumps(list(v.tags), ensure_ascii=False), v.review)
                for v in library.viewings.values()
            ],
        )
        conn.executemany(
            "INSERT INTO entry_link VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (l.source, l.entry_uri, l.name, l.year, l.status, l.film_uri, json.dumps(list(l.candidates)))
                for l in library.entry_links
            ],
        )
        conn.executemany(
            "INSERT INTO list VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (lst.slug, lst.name, lst.date, json.dumps(list(lst.tags), ensure_ascii=False), lst.url,
                 lst.description, json.dumps(sorted(library.list_roles.get(lst.slug, ()))))
                for lst in library.lists.values()
            ],
        )
        conn.executemany(
            "INSERT INTO list_entry VALUES (?, ?, ?, ?)",
            [
                (lst.slug, e.position, e.uri, e.note)
                for lst in library.lists.values()
                for e in lst.entries
                if e.uri
            ],
        )
        conn.executemany(
            "INSERT INTO orphan VALUES (?, ?, ?, ?)",
            [(o.source, o.name, o.year, json.dumps(list(o.candidates))) for o in library.orphans],
        )
        # Every film gets an identity row; existing ones (and their history) are kept.
        conn.execute(
            "INSERT OR IGNORE INTO identity (uri, status) SELECT uri, ? FROM film", (PENDING,)
        )
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('export_source', ?)", (source,))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('imported_at', ?)", (now(),))


def export_counts(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "films": one("SELECT COUNT(*) FROM film"),
        "watched": one("SELECT COUNT(*) FROM film WHERE watched"),
        "watchlist_csv": one("SELECT COUNT(*) FROM film WHERE on_watchlist"),
        "watched_and_watchlist_csv": one("SELECT COUNT(*) FROM film WHERE watched AND on_watchlist"),
        "to_watch": one(
            "SELECT COUNT(*) FROM film WHERE NOT watched"
            " AND (on_watchlist OR EXISTS (SELECT 1 FROM json_each(film.roles) WHERE value = 'watchlist'))"
        ),
        "diary_rows": one("SELECT COUNT(*) FROM entry_link WHERE source = 'diary.csv'"),
        "diary_attached": one("SELECT COUNT(*) FROM entry_link WHERE source = 'diary.csv' AND status = 'attached'"),
        "reviews_rows": one("SELECT COUNT(*) FROM entry_link WHERE source = 'reviews.csv'"),
        "reviews_attached": one(
            "SELECT COUNT(*) FROM entry_link WHERE source = 'reviews.csv' AND status = 'attached'"
        ),
        "lists": one("SELECT COUNT(*) FROM list"),
        "list_entries": one("SELECT COUNT(*) FROM list_entry"),
        "orphans": one("SELECT COUNT(*) FROM orphan"),
    }


def identity_counts(conn: sqlite3.Connection) -> dict:
    by_status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM identity GROUP BY status")}
    by_reason = {
        f"{r[0]}:{r[1]}": r[2]
        for r in conn.execute(
            "SELECT status, reason, COUNT(*) FROM identity WHERE reason IS NOT NULL"
            " AND status != 'confirmed' GROUP BY status, reason ORDER BY status, reason"
        )
    }
    by_method = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT method, COUNT(*) FROM identity WHERE status = 'confirmed' GROUP BY method ORDER BY method"
        )
    }
    by_type = {
        r[0] or "none": r[1]
        for r in conn.execute(
            "SELECT tmdb_type, COUNT(*) FROM identity WHERE status = 'confirmed' GROUP BY tmdb_type"
        )
    }
    return {"by_status": by_status, "by_reason": by_reason, "confirmed_by_method": by_method,
            "confirmed_by_type": by_type}
