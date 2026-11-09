from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from boxd_match import db, local, localtags
from boxd_match.config import load_config
from boxd_match.tmdb import TmdbClient
from fakes import TMDB, FakeTransport, find, movie, ok, tv


def tmdb_routes() -> dict:
    return {
        f"{TMDB}/find/tt1160419": ok(find(movies=[438631])),
        f"{TMDB}/movie/438631": ok(movie(438631, "Dune", "2021-09-15")),
        f"{TMDB}/movie/949": ok(movie(949, "Heat", "1995-12-15")),
        # "Flagrant délit (1960)" tagged with The Apartment's IMDb id.
        f"{TMDB}/find/tt0053604": ok(find(movies=[284])),
        f"{TMDB}/movie/284": ok(movie(284, "The Apartment", "1960-06-21", translations=["La Garçonnière"])),
        f"{TMDB}/tv/1920": ok(tv(1920, "Twin Peaks", "1990-04-08")),
        f"{TMDB}/find/tt9059760": ok(find(tvs=[89905])),
        f"{TMDB}/tv/89905": ok(tv(89905, "Normal People", "2020-04-26")),
    }


@pytest.fixture
def scanned(library_config_file: Path, tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "t.sqlite")
    local.scan(conn, load_config(library_config_file).roots)
    return conn


def _tags(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["tag_key"]: r for r in conn.execute("SELECT * FROM local_tag")}


def test_check_tags(scanned: sqlite3.Connection) -> None:
    fake = FakeTransport(tmdb_routes())
    progress = localtags.check_tags(scanned, TmdbClient(fake, "k", scanned))
    assert progress.counts == {"ok": 4, "suspect:title_mismatch": 1}
    tags = _tags(scanned)
    assert (tags["imdb:tt1160419"]["tmdb_type"], tags["imdb:tt1160419"]["tmdb_id"]) == ("movie", "438631")
    assert tags["tmdb:949"]["status"] == "ok"
    flagrant = tags["imdb:tt0053604"]
    assert (flagrant["status"], flagrant["reason"], flagrant["tmdb_id"], flagrant["title"]) == (
        "suspect", "title_mismatch", "284", "Flagrant délit")
    assert (tags["tmdb:1920"]["tmdb_type"], tags["imdb:tt9059760"]["tmdb_id"]) == ("tv", "89905")
    # Already checked tags are not asked again.
    calls = len(fake.calls)
    assert localtags.check_tags(scanned, TmdbClient(fake, "k", scanned)).done == 0
    assert len(fake.calls) == calls


def test_unknown_and_wrong_type_tags(scanned: sqlite3.Connection) -> None:
    routes = tmdb_routes()
    del routes[f"{TMDB}/movie/949"]  # TMDB answers 404
    routes[f"{TMDB}/find/tt1160419"] = ok(find(tvs=[5]))  # a movie folder tagged with a series id
    localtags.check_tags(scanned, TmdbClient(FakeTransport(routes), "k", scanned))
    tags = _tags(scanned)
    assert (tags["tmdb:949"]["status"], tags["tmdb:949"]["reason"]) == ("not_found", "tmdb_not_found")
    assert (tags["imdb:tt1160419"]["status"], tags["imdb:tt1160419"]["reason"]) == ("suspect", "type_mismatch")


def test_recheck(scanned: sqlite3.Connection) -> None:
    localtags.check_tags(scanned, TmdbClient(FakeTransport(tmdb_routes()), "k", scanned))
    assert localtags.check_tags(scanned, TmdbClient(FakeTransport(tmdb_routes()), "k", scanned), recheck=True).done == 5
