"""TMDB v3 client with a permanent cache in the database.

Only 200 and 404 answers are cached. The API key never appears in cache keys,
errors or output.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from boxd_match.db import now
from boxd_match.net import Transport, with_query

API = "https://api.themoviedb.org/3"
MAX_RETRIES = 3


class TmdbError(Exception):
    pass


def load_api_key(env_file: str | Path = ".env") -> str:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key and Path(env_file).is_file():
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "TMDB_API_KEY":
                key = value.strip().strip('"').strip("'")
    if not key:
        raise TmdbError("TMDB_API_KEY is not set (environment or .env)")
    return key


class TmdbClient:
    def __init__(
        self,
        transport: Transport,
        api_key: str,
        conn: sqlite3.Connection,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._key = api_key
        self._conn = conn
        self._sleep = sleep
        self.requests = 0

    def _get(self, path: str, params: dict[str, str]) -> tuple[int, dict | None]:
        url = with_query(f"{API}/{path}", {**params, "api_key": self._key})
        for attempt in range(MAX_RETRIES + 1):
            response = self._transport.get(url)
            self.requests += 1
            if response.status == 429 and attempt < MAX_RETRIES:
                self._sleep(float(response.headers.get("Retry-After", "2") or 2))
                continue
            break
        if response.status == 401:
            raise TmdbError("TMDB rejected the API key (HTTP 401)")
        if response.status == 200:
            return 200, json.loads(response.body)
        if response.status == 404:
            return 404, None
        raise TmdbError(f"TMDB answered HTTP {response.status} for /{path}")

    def _cached(self, table: str, column: str, key: str, path: str, params: dict[str, str]):
        row = self._conn.execute(f"SELECT http_status, body FROM {table} WHERE {column} = ?", (key,)).fetchone()
        if row is not None:
            return row[0], (json.loads(row[1]) if row[1] else None)
        status, body = self._get(path, params)
        self._conn.execute(
            f"INSERT OR REPLACE INTO {table} ({column}, http_status, body, fetched_at) VALUES (?, ?, ?, ?)",
            (key, status, json.dumps(body, ensure_ascii=False) if body is not None else None, now()),
        )
        self._conn.commit()
        return status, body

    def details(self, tmdb_type: str, tmdb_id: str) -> tuple[int, dict | None]:
        key = f"{tmdb_type}/{tmdb_id}"
        return self._cached(
            "tmdb_cache", "key", key, key, {"append_to_response": "external_ids,translations"}
        )

    def find(self, imdb_id: str) -> tuple[int, dict | None]:
        return self._cached(
            "find_cache", "imdb_id", imdb_id, f"find/{imdb_id}", {"external_source": "imdb_id"}
        )
