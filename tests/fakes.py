"""Fake transport and synthetic TMDB / Letterboxd answers. No network."""

from __future__ import annotations

import json
import urllib.parse

from boxd_match.net import Response


class FakeTransport:
    """Serves canned responses keyed by URL path (query string ignored).

    A value may be a list: responses are served in order, the last one repeats.
    """

    def __init__(self, routes: dict[str, Response | list[Response]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> Response:
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        key = f"{parts.netloc}{parts.path}"
        route = self.routes.get(key)
        if route is None:
            return Response(404, url, b"")
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route

    def paths(self) -> list[str]:
        return [urllib.parse.urlsplit(u).path for u in self.calls]


def ok(body: dict, url: str = "", headers: dict | None = None) -> Response:
    return Response(200, url, json.dumps(body).encode(), headers or {})


def status(code: int, headers: dict | None = None) -> Response:
    return Response(code, "", b"", headers or {})


def movie(tmdb_id: int, title: str, date: str, original: str | None = None, translations=(), imdb=None) -> dict:
    return {
        "id": tmdb_id, "title": title, "original_title": original or title, "release_date": date,
        "external_ids": {"imdb_id": imdb},
        "translations": {"translations": [{"data": {"title": t}} for t in translations]},
    }


def tv(tmdb_id: int, name: str, date: str, imdb=None) -> dict:
    return {
        "id": tmdb_id, "name": name, "original_name": name, "first_air_date": date,
        "external_ids": {"imdb_id": imdb}, "translations": {"translations": []},
    }


def find(movies=(), tvs=()) -> dict:
    return {
        "movie_results": [{"id": i} for i in movies],
        "tv_results": [{"id": i} for i in tvs],
        "tv_episode_results": [],
    }


TMDB = "api.themoviedb.org/3"
