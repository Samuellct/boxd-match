from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from boxd_match import db, identity as idt, resolver
from boxd_match.cli import main
from boxd_match.config import Config
from boxd_match.export import read_export
from boxd_match.films import build_library
from boxd_match.tmdb import TmdbClient, TmdbError, load_api_key
from fakes import TMDB, FakeTransport, find, movie, ok, status, tv


def check(details, *, name="Dune", year="2021", tmdb_type="movie", tmdb_id="438631",
          find_body=None, imdb=None, require_find=False, details_status=200):
    return idt.check_candidate(
        name=name, year=year, tmdb_type=tmdb_type, tmdb_id=tmdb_id, details_status=details_status,
        details=details, find_body=find_body, imdb_id=imdb, require_find=require_find,
    )


# Pure decision logic


def test_normalize() -> None:
    assert idt.normalize("Amélie") == idt.normalize("AMELIE")
    assert idt.normalize("Mission: Impossible – Dead Reckoning") == "mission impossible dead reckoning"
    assert idt.normalize("Fast & Furious") == "fast and furious"


def test_title_and_year_confirm() -> None:
    v = check(movie(438631, "Dune", "2021-09-15"))
    assert (v.status, v.method) == (db.CONFIRMED, idt.BY_TITLE_YEAR)


def test_find_confirms_even_with_other_title() -> None:
    v = check(movie(438631, "Dune: Part One", "2021-09-15"), find_body=find(movies=[438631]), imdb="tt1160419")
    assert (v.status, v.method, v.imdb_id) == (db.CONFIRMED, idt.BY_FIND, "tt1160419")


def test_title_found_through_translation_or_original() -> None:
    assert check(movie(1, "Spirited Away", "2001-07-20", original="千と千尋の神隠し"), name="千と千尋の神隠し",
                 year="2001", tmdb_id="1").status == db.CONFIRMED
    assert check(movie(2, "The Untouchables", "2011-11-02", translations=["Intouchables"]), name="Intouchables",
                 year="2011", tmdb_id="2").status == db.CONFIRMED


@pytest.mark.parametrize(("lb_year", "expected"), [("2019", db.CONFIRMED), ("2023", db.CONFIRMED),
                                                     ("2018", db.TO_REVIEW), ("", db.TO_REVIEW)])
def test_year_tolerance_is_two(lb_year: str, expected: str) -> None:
    assert check(movie(438631, "Dune", "2021-09-15"), year=lb_year).status == expected


def test_series_fields() -> None:
    v = check(tv(89905, "Normal People", "2020-04-26"), name="Normal People", year="2020",
              tmdb_type="tv", tmdb_id="89905")
    assert v.status == db.CONFIRMED


def test_title_mismatch() -> None:
    v = check(movie(438631, "Dune", "2021-09-15"), name="Arrival")
    assert (v.status, v.reason) == (db.TO_REVIEW, idt.TITLE_MISMATCH)


def test_find_pointing_elsewhere_blocks_confirmation() -> None:
    v = check(movie(438631, "Dune", "2021-09-15"), find_body=find(movies=[841]), imdb="tt0087182")
    assert (v.status, v.reason) == (db.TO_REVIEW, idt.FIND_MISMATCH)


def test_legacy_needs_find_and_detects_series() -> None:
    # A miniseries cached as movie/123: /find says it is tv/456.
    v = check(movie(123, "Chernobyl", "2019-05-06"), name="Chernobyl", year="2019", tmdb_id="123",
              find_body=find(tvs=[456]), imdb="tt7366338", require_find=True)
    assert (v.status, v.reason) == (db.TO_REVIEW, idt.TYPE_MISMATCH)
    # Same title and year but no IMDb to settle the type: not confirmed.
    v = check(movie(438631, "Dune", "2021-09-15"), require_find=True)
    assert (v.status, v.reason) == (db.TO_REVIEW, idt.TYPE_UNVERIFIED)


def test_not_found() -> None:
    v = check(None, details_status=404)
    assert (v.status, v.reason) == (db.TO_REVIEW, idt.TMDB_NOT_FOUND)


# TMDB client


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.sqlite")


def test_client_caches_and_hides_key(conn: sqlite3.Connection) -> None:
    fake = FakeTransport({f"{TMDB}/movie/438631": ok(movie(438631, "Dune", "2021-09-15"))})
    client = TmdbClient(fake, "SECRETKEY", conn)
    assert client.details("movie", "438631")[0] == 200
    assert client.details("movie", "438631")[0] == 200
    assert len(fake.calls) == 1
    assert "append_to_response=external_ids%2Ctranslations" in fake.calls[0]
    dump = "\n".join(conn.iterdump())
    assert "SECRETKEY" not in dump and "movie/438631" in dump


def test_client_404_is_cached(conn: sqlite3.Connection) -> None:
    fake = FakeTransport({})
    client = TmdbClient(fake, "k", conn)
    assert client.details("tv", "1") == (404, None)
    assert client.details("tv", "1") == (404, None)
    assert len(fake.calls) == 1


def test_client_retries_after_429(conn: sqlite3.Connection) -> None:
    slept: list[float] = []
    fake = FakeTransport({f"{TMDB}/find/tt1": [status(429, {"Retry-After": "3"}), ok(find(movies=[1]))]})
    client = TmdbClient(fake, "k", conn, sleep=slept.append)
    assert client.find("tt1")[0] == 200
    assert slept == [3.0]


def test_client_bad_key(conn: sqlite3.Connection) -> None:
    with pytest.raises(TmdbError, match="401"):
        TmdbClient(FakeTransport({f"{TMDB}/movie/1": status(401)}), "k", conn).details("movie", "1")


def test_load_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('OTHER=1\nTMDB_API_KEY="abc"\n', encoding="utf-8")
    assert load_api_key(env) == "abc"
    monkeypatch.setenv("TMDB_API_KEY", "fromenv")
    assert load_api_key(env) == "fromenv"
    monkeypatch.delenv("TMDB_API_KEY")
    with pytest.raises(TmdbError):
        load_api_key(tmp_path / "missing.env")


# Seed and legacy check on the synthetic export


def _imported(export_dir: Path, path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    db.import_library(conn, build_library(read_export(export_dir), Config()), "x")
    return conn


LEGACY_CACHE = {
    "https://boxd.it/f1": {"page": "https://letterboxd.com/film/dune-2021/", "imdb": "tt1160419", "tmdb": "438631"},
    "https://boxd.it/f2": {"page": "https://letterboxd.com/film/heat/", "imdb": "tt0113277", "tmdb": "949"},
    # A miniseries the old resolver stored as a movie id.
    "https://boxd.it/f6": {"page": "https://letterboxd.com/film/alien/", "imdb": "tt9999999", "tmdb": "348"},
    "https://boxd.it/f7": {"page": "https://letterboxd.com/film/stalker/", "imdb": "tt0079944", "tmdb": None},
    "https://boxd.it/zz": {"page": "https://letterboxd.com/film/other/", "imdb": "tt1", "tmdb": "1"},
    "https://boxd.it/err": {"error": "HTTP Error 500"},
}


def test_seed_never_confirms(export_dir: Path, tmp_path: Path) -> None:
    conn = _imported(export_dir, tmp_path / "t.sqlite")
    cache = tmp_path / "ids.json"
    cache.write_text(json.dumps(LEGACY_CACHE), encoding="utf-8")
    r = resolver.seed_legacy(conn, cache)
    assert (r.seeded, r.not_in_export, r.errors) == (4, 1, 1)
    row = conn.execute("SELECT * FROM identity WHERE uri = 'https://boxd.it/f1'").fetchone()
    assert (row["source"], row["status"], row["tmdb_type"], row["slug"]) == ("legacy", db.PENDING, None, "dune-2021")


def test_check_legacy(export_dir: Path, tmp_path: Path) -> None:
    conn = _imported(export_dir, tmp_path / "t.sqlite")
    cache = tmp_path / "ids.json"
    cache.write_text(json.dumps(LEGACY_CACHE), encoding="utf-8")
    resolver.seed_legacy(conn, cache)
    fake = FakeTransport({
        f"{TMDB}/movie/438631": ok(movie(438631, "Dune", "2021-09-15")),
        f"{TMDB}/find/tt1160419": ok(find(movies=[438631])),
        f"{TMDB}/movie/949": ok(movie(949, "Heat", "1995-12-15")),
        f"{TMDB}/find/tt0113277": ok(find(movies=[949])),
        f"{TMDB}/movie/348": ok(movie(348, "Alien", "1979-05-25")),
        f"{TMDB}/find/tt9999999": ok(find(tvs=[1234])),
    })
    progress = resolver.check_legacy(conn, TmdbClient(fake, "k", conn))
    assert progress.counts == {"confirmed": 2, "needs_page:type_mismatch": 1, "needs_page:no_tmdb_id": 1}
    rows = {r["uri"]: r for r in conn.execute("SELECT * FROM identity")}
    assert (rows["https://boxd.it/f1"]["tmdb_type"], rows["https://boxd.it/f1"]["method"]) == ("movie", "imdb_find")
    assert rows["https://boxd.it/f6"]["status"] == db.NEEDS_PAGE
    # Films without legacy data stay pending for the page phase.
    assert rows["https://boxd.it/f9"]["status"] == db.PENDING
    # Re-running does nothing more: everything is cached or final.
    calls = len(fake.calls)
    assert resolver.check_legacy(conn, TmdbClient(fake, "k", conn)).done == 0
    assert len(fake.calls) == calls


def test_cli_coverage(export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "t.sqlite"
    _imported(export_dir, path).close()
    assert main(["coverage", "--db", str(path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["by_status"] == {"pending": 8}
    assert len(out["films"]) == 8


@pytest.mark.parametrize(("local", "tmdb"), [
    ("Margos Got Money Troubles", "Margo's Got Money Troubles"),
    ("Good Luck Have Fun Dont Die", "Good Luck, Have Fun, Don’t Die"),
    ("Fahrenheit 911", "Fahrenheit 9/11"),
])
def test_titles_match_ignores_apostrophes_and_spacing(local: str, tmdb: str) -> None:
    assert idt.titles_match(local, {idt.normalize(tmdb)})


def test_titles_match_still_rejects_other_titles() -> None:
    assert not idt.titles_match("Mission Impossible 1", {idt.normalize("Mission: Impossible")})
    assert not idt.titles_match("Flagrant délit", {idt.normalize("The Apartment"), idt.normalize("La Garçonnière")})
