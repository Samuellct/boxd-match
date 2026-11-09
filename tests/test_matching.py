from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from boxd_match import db, local, localtags, matching
from boxd_match.cli import main
from boxd_match.config import load_config
from boxd_match.export import read_export
from boxd_match.films import build_library
from boxd_match.tmdb import TmdbClient
from fakes import TMDB, FakeTransport, ok, tv
from test_localtags import tmdb_routes


def _routes() -> dict:
    routes = tmdb_routes()
    twin = tv(1920, "Twin Peaks", "1990-04-08")
    twin["number_of_episodes"] = 48
    twin["seasons"] = [{"season_number": 0, "episode_count": 3}, {"season_number": 3, "episode_count": 18}]
    routes[f"{TMDB}/tv/1920"] = ok(twin)
    normal = tv(89905, "Normal People", "2020-04-26")
    normal["number_of_episodes"] = 2
    routes[f"{TMDB}/tv/89905"] = ok(normal)
    return routes


IDENTITIES = {
    # film uri: (tmdb_type, tmdb_id, imdb_id, season, episode)
    "https://boxd.it/f1": ("movie", "438631", "tt1160419", None, None),  # Dune, in two roots
    "https://boxd.it/f2": ("movie", "949", None, None, None),            # Heat, tmdbid tag
    "https://boxd.it/f4": ("movie", "284", "tt0053604", None, None),     # The Apartment: only a suspect local tag
    "https://boxd.it/f6": ("tv", "1920", None, 3, 2),                    # one episode, present
    "https://boxd.it/f7": ("tv", "1920", None, 3, None),                 # season 3, 2 of 18 episodes
    "https://boxd.it/f8": ("tv", "89905", "tt9059760", None, None),      # whole miniseries, complete
    "https://boxd.it/f9": ("movie", "655", None, None, None),            # Paris, Texas: local copy untagged
}


@pytest.fixture
def conn(export_dir: Path, library_config_file: Path, tmp_path: Path) -> sqlite3.Connection:
    config = load_config(library_config_file)
    c = db.connect(tmp_path / "t.sqlite")
    db.import_library(c, build_library(read_export(export_dir), config), "x")
    for uri, (tmdb_type, tmdb_id, imdb_id, season, episode) in IDENTITIES.items():
        c.execute(
            "UPDATE identity SET status = 'confirmed', tmdb_type = ?, tmdb_id = ?, imdb_id = ?, tmdb_season = ?,"
            " tmdb_episode = ? WHERE uri = ?", (tmdb_type, tmdb_id, imdb_id, season, episode, uri))
    c.commit()
    local.scan(c, config.roots)
    return c


def _check(conn: sqlite3.Connection) -> None:
    localtags.check_tags(conn, TmdbClient(FakeTransport(_routes()), "k", conn))


def _matches(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["uri"].rsplit("/", 1)[1]: r for r in conn.execute("SELECT * FROM local_match")}


def test_match_states(conn: sqlite3.Connection) -> None:
    _check(conn)
    result = matching.match(conn)
    m = _matches(conn)
    dune = m["f1"]
    assert (dune["state"], dune["method"], json.loads(dune["resolutions"])) == ("present", "tmdb", ["1080p", "2160p"])
    assert len(json.loads(dune["files"])) == 2
    assert m["f2"]["state"] == "present"
    assert m["f4"]["state"] == "absent"  # suspect tag never counts
    assert m["f6"]["state"] == "present"
    assert len(json.loads(m["f6"]["files"])) == 1
    assert (m["f7"]["state"], m["f7"]["episodes_local"], m["f7"]["episodes_total"]) == ("partial", 2, 18)
    assert (m["f8"]["state"], m["f8"]["episodes_local"], m["f8"]["episodes_total"]) == ("present", 2, 2)
    assert m["f9"]["state"] == "absent"
    assert result.states == {"present": 4, "partial": 1, "absent": 2}
    assert [i.tag_key for i in result.flagged_tags] == ["imdb:tt0053604"]
    assert result.local_only == []
    # Untagged copy of Paris, Texas: a suggestion, never a match.
    (paris,) = [s for s in result.suggestions if s["title"] == "Paris, Texas"]
    assert paris["candidates"] == ["https://boxd.it/f9"]
    assert result.unchecked_used == 0


def test_unchecked_tags_match_on_raw_ids(conn: sqlite3.Connection) -> None:
    result = matching.match(conn)  # no check-tags yet
    m = _matches(conn)
    assert m["f1"]["state"] == "present" and m["f1"]["method"] == "imdb"
    assert m["f2"]["state"] == "present" and m["f2"]["method"] == "tmdb"
    assert result.unchecked_used >= 2


def test_local_only_items(conn: sqlite3.Connection) -> None:
    _check(conn)
    conn.execute("UPDATE identity SET status = 'to_review' WHERE uri = 'https://boxd.it/f2'")
    conn.commit()
    result = matching.match(conn)
    assert [i.tag_key for i in result.local_only] == ["tmdb:949"]


def test_breakdown_and_cli(conn: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _check(conn)
    conn.close()
    db_path = str(tmp_path / "t.sqlite")
    assert main(["match", "--db", db_path, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["states"] == {"present": 4, "partial": 1, "absent": 2}
    assert out["by_letterboxd_status"]["watched"] == {"present": 3, "absent": 1}  # Dune, Heat, Amélie=f8; Crash f4
    assert out["flagged_tags"][0]["title"] == "Flagrant délit"
    assert main(["match", "--db", db_path]) == 0
    text = capsys.readouterr().out
    assert "partial series (1)" in text and "flagged local tags (never matched) (1)" in text


@pytest.mark.parametrize(("relative", "expected"), [
    ("Paris, Texas (1984)/Paris.Texas.1984.MULTi.1080p.mkv", ("Paris, Texas", 1984)),
    ("Bad.Taste.1987.MULTi.1080p.mkv", ("Bad Taste", 1987)),
    ("Hunger 2008 MULTi 1080p.mkv", ("Hunger", 2008)),
    ("Untagged Movie (2001).mkv", ("Untagged Movie", 2001)),
    ("random.mkv", ("random", None)),
])
def test_guess_title_year(relative: str, expected: tuple) -> None:
    assert matching.guess_title_year(relative) == expected
