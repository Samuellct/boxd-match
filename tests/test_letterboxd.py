from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from boxd_match import db, resolver
from boxd_match.config import Config
from boxd_match.export import read_export
from boxd_match.films import build_library
from boxd_match.letterboxd import is_challenge, parse_film_page
from boxd_match.net import NetworkError, Response
from boxd_match.tmdb import TmdbClient
from fakes import TMDB, FakeTransport, find, movie, ok, tv


def page(slug: str, link: str | None, attr: str = "", imdb: str | None = None) -> str:
    """Synthetic film page shaped like the real one (attribute order included)."""
    parts = [f'<html><head><title>&lrm;{slug} • Letterboxd</title></head>',
             f'<body class="film backdropped" data-type="film" data-tmdb-type="movie" data-tmdb-id="{attr}">']
    if imdb:
        parts.append(f'<a href="http://www.imdb.com/title/{imdb}/maindetails" class="micro-button track-event"'
                     ' data-track-action="IMDb" target="_blank" >IMDb</a>')
    if link:
        parts.append(f'<a href="https://www.themoviedb.org/{link}/" class="micro-button track-event"'
                     ' data-track-action="TMDB" target="_blank" >TMDB</a>')
    # Other films on the page must not be read as this film's ids.
    parts.append('<div data-item-slug="interstellar"><a href="https://www.themoviedb.org/movie/157336/">x</a></div>')
    return "\n".join(parts) + "</body></html>"


def html(slug: str, body: str, status: int = 200) -> Response:
    return Response(status, f"https://letterboxd.com/film/{slug}/", body.encode())


# Parser


def test_parse_movie() -> None:
    p = parse_film_page("https://letterboxd.com/film/dune-2021/", page("dune", "movie/438631", "438631", "tt1160419"))
    assert (p.slug, p.link_type, p.link_id, p.attr_id, p.imdb_id) == ("dune-2021", "movie", "438631", "438631", "tt1160419")
    assert not p.conflict


def test_parse_miniseries_with_empty_attribute() -> None:
    p = parse_film_page("https://letterboxd.com/film/normal-people-2020/", page("np", "tv/89905", "", "tt9059760"))
    assert (p.link_type, p.link_id, p.attr_id) == ("tv", "89905", None)
    assert not p.conflict


def test_parse_rejects_tv_zero_and_flags_conflict() -> None:
    assert parse_film_page("u", page("x", "tv/0", "")).link_id is None
    assert parse_film_page("u", page("x", "movie/1", "2")).conflict


def test_parse_without_tmdb_link_ignores_other_links() -> None:
    p = parse_film_page("u", page("x", None, ""))
    assert (p.link_id, p.attr_id) == (None, None)


def test_challenge_detection() -> None:
    assert is_challenge(Response(200, "u", b"<html><head><title>Just a moment...</title>"))
    assert is_challenge(Response(403, "u", b"", {"cf-mitigated": "challenge"}))
    assert not is_challenge(Response(200, "u", page("x", "movie/1").encode()))


# Fetcher and full resolution on the synthetic export


@pytest.fixture
def conn(export_dir: Path, tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "t.sqlite")
    db.import_library(c, build_library(read_export(export_dir), Config()), "x")
    return c


def _tmdb(conn: sqlite3.Connection) -> TmdbClient:
    return TmdbClient(FakeTransport({
        f"{TMDB}/movie/438631": ok(movie(438631, "Dune", "2021-09-15")),
        f"{TMDB}/find/tt1160419": ok(find(movies=[438631])),
        f"{TMDB}/movie/949": ok(movie(949, "Heat", "1995-12-15")),
        f"{TMDB}/tv/89905": ok(tv(89905, "Paris, Texas", "1984-05-19")),
        f"{TMDB}/movie/348": ok(movie(348, "Alien", "1979-05-25")),
        f"{TMDB}/movie/1": ok(movie(1, "Crash", "2004-09-10")),
        f"{TMDB}/movie/2": ok(movie(2, "Crash", "1996-10-04")),
    }), "k", conn)


PAGES = {
    "boxd.it/f1": html("dune-2021", page("dune", "movie/438631", "438631", "tt1160419")),
    "boxd.it/f2": html("heat", page("heat", "movie/949", "949")),
    "boxd.it/f4": html("crash-2004", page("crash", "movie/1", "1")),
    "boxd.it/f5": html("crash-2004-1", page("crash", "movie/2", "2")),  # year 1996 vs 2004
    "boxd.it/f6": html("alien", page("alien", "movie/348", "999")),  # link/attribute conflict
    "boxd.it/f7": html("stalker", page("stalker", None, "")),  # no TMDB link
    "boxd.it/f8": Response(404, "https://letterboxd.com/film/amelie/", b""),
    "boxd.it/f9": html("paris-texas", page("pt", "tv/89905", "")),
    # Diary entry e3 (Crash, ambiguous by title) redirects to the 2004 film page.
    "boxd.it/e3": Response(200, "https://letterboxd.com/tester/film/crash-2004/", b"<html></html>"),
}


def test_full_resolution(conn: sqlite3.Connection) -> None:
    fake = FakeTransport(dict(PAGES))
    fetcher = resolver.PageFetcher(conn, fake, sleep=lambda s: None)
    progress = resolver.read_pages(conn, fetcher, _tmdb(conn))
    assert progress.done == 8
    rows = {r["uri"].rsplit("/", 1)[1]: r for r in conn.execute("SELECT * FROM identity")}
    assert (rows["f1"]["status"], rows["f1"]["method"]) == ("confirmed", "imdb_find")
    assert (rows["f2"]["status"], rows["f2"]["method"]) == ("confirmed", "title_year")
    assert (rows["f9"]["status"], rows["f9"]["tmdb_type"], rows["f9"]["tmdb_id"]) == ("confirmed", "tv", "89905")
    assert (rows["f5"]["status"], rows["f5"]["reason"]) == ("to_review", "year_mismatch")
    assert (rows["f6"]["status"], rows["f6"]["reason"]) == ("to_review", "link_attr_conflict")
    assert (rows["f7"]["status"], rows["f7"]["reason"]) == ("unresolved", "no_tmdb_link")
    assert (rows["f8"]["status"], rows["f8"]["reason"]) == ("unresolved", "page_not_found")
    assert resolver.non_final(conn) == 0
    # Second run: nothing to fetch.
    calls = len(fake.calls)
    assert resolver.read_pages(conn, fetcher, _tmdb(conn)).done == 0
    assert len(fake.calls) == calls
    # The ambiguous diary entry is attached through its redirect slug.
    assert resolver.attach_redirects(conn, fetcher) == 1
    link = conn.execute("SELECT status, film_uri FROM entry_link WHERE entry_uri = 'https://boxd.it/e3'").fetchone()
    assert tuple(link) == ("attached", "https://boxd.it/f4")
    viewing = conn.execute("SELECT film_uri FROM viewing WHERE entry_uri = 'https://boxd.it/e3'").fetchone()
    assert viewing[0] == "https://boxd.it/f4"


def test_redirect_survives_reimport_without_network(conn: sqlite3.Connection, export_dir: Path) -> None:
    fetcher = resolver.PageFetcher(conn, FakeTransport(dict(PAGES)), sleep=lambda s: None)
    resolver.read_pages(conn, fetcher, _tmdb(conn))
    resolver.attach_redirects(conn, fetcher)
    db.import_library(conn, build_library(read_export(export_dir), Config()), "x")
    assert resolver.attach_redirects(conn) == 1  # from page_cache only


@pytest.mark.parametrize("response", [
    Response(403, "u", b""),
    Response(503, "u", b""),
    Response(200, "u", b"<html><head><title>Just a moment...</title></head>"),
])
def test_block_stops_and_resume_continues(conn: sqlite3.Connection, response: Response) -> None:
    routes = dict(PAGES)
    routes["boxd.it/f4"] = response
    fetcher = resolver.PageFetcher(conn, FakeTransport(routes), sleep=lambda s: None)
    with pytest.raises(resolver.Blocked):
        resolver.read_pages(conn, fetcher, _tmdb(conn))
    done_before = conn.execute("SELECT COUNT(*) FROM identity WHERE status IN ('confirmed','to_review','unresolved')").fetchone()[0]
    assert done_before == 2  # f1 and f2 were saved before the block on f4
    # Block lifted: the next run resumes and finishes.
    fetcher = resolver.PageFetcher(conn, FakeTransport(dict(PAGES)), sleep=lambda s: None)
    assert resolver.read_pages(conn, fetcher, _tmdb(conn)).done == 6
    assert resolver.non_final(conn) == 0


def test_429_waits_once_then_blocks_on_second(conn: sqlite3.Connection) -> None:
    slept: list[float] = []
    routes = dict(PAGES)
    routes["boxd.it/f1"] = [Response(429, "u", b"", {"Retry-After": "7"}), PAGES["boxd.it/f1"]]
    fetcher = resolver.PageFetcher(conn, FakeTransport(routes), sleep=slept.append)
    assert fetcher.get("https://boxd.it/f1")["link_id"] == "438631"
    assert slept == [7.0]
    routes["boxd.it/f2"] = Response(429, "u", b"", {"Retry-After": "1"})
    with pytest.raises(resolver.Blocked, match="429"):
        fetcher.get("https://boxd.it/f2")


class FlakyTransport:
    def get(self, url: str) -> Response:
        raise NetworkError("letterboxd.com: timed out")


def test_consecutive_network_failures_block(conn: sqlite3.Connection) -> None:
    fetcher = resolver.PageFetcher(conn, FlakyTransport(), sleep=lambda s: None)
    with pytest.raises(resolver.Blocked, match="5 failures"):
        resolver.read_pages(conn, fetcher, _tmdb(conn))
    assert resolver.non_final(conn) == 8  # nothing half-written


def test_legacy_type_mismatch_is_fixed_by_the_page(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cache = tmp_path / "ids.json"
    cache.write_text(json.dumps({"https://boxd.it/f9": {
        "page": "https://letterboxd.com/film/paris-texas/", "imdb": "tt0087884", "tmdb": "655"}}), encoding="utf-8")
    resolver.seed_legacy(conn, cache)
    client = TmdbClient(FakeTransport({
        f"{TMDB}/movie/655": ok(movie(655, "Paris, Texas", "1984-05-19")),
        f"{TMDB}/find/tt0087884": ok(find(tvs=[89905])),
        f"{TMDB}/tv/89905": ok(tv(89905, "Paris, Texas", "1984-05-19")),
    }), "k", conn)
    resolver.check_legacy(conn, client)
    row = conn.execute("SELECT status, reason FROM identity WHERE uri = 'https://boxd.it/f9'").fetchone()
    assert tuple(row) == ("needs_page", "type_mismatch")
    routes = {"boxd.it/f9": html("paris-texas", page("pt", "tv/89905", "", "tt0087884"))}
    resolver.read_pages(conn, resolver.PageFetcher(conn, FakeTransport(routes), sleep=lambda s: None), client)
    row = conn.execute("SELECT status, source, tmdb_type, tmdb_id FROM identity WHERE uri = 'https://boxd.it/f9'").fetchone()
    assert tuple(row) == ("confirmed", "page", "tv", "89905")
