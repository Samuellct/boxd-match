from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest

from boxd_match import localtags, matching, review
from boxd_match.tmdb import TmdbClient
from fakes import FakeTransport
from test_matching import _check, _matches, _routes, conn  # noqa: F401  (fixture)


def _decide(path: Path, decision: str) -> None:
    text = path.read_text(encoding="utf-8")
    head, sep, tags = text.partition("[[tag]]")
    path.write_text(head + sep + tags.replace('decision = ""', f'decision = "{decision}"', 1), encoding="utf-8")


def test_suspect_tag_in_review_file(conn: sqlite3.Connection, tmp_path: Path) -> None:  # noqa: F811
    _check(conn)
    out = tmp_path / "review.toml"
    result = review.export_review(conn, out)
    assert result.tags == 1
    (tag,) = tomllib.loads(out.read_text(encoding="utf-8"))["tag"]
    assert (tag["tag"], tag["kind"], tag["local_name"], tag["reason"]) == (
        "imdb:tt0053604", "movies", "Flagrant délit (1960)", "title_mismatch")
    assert tag["tmdb"].startswith("movie/284  The Apartment (1960)")
    assert tag["files"][0].endswith("Flagrant délit (1960) [imdbid-tt0053604].mkv")


def test_accepting_a_tag_makes_the_film_present(conn: sqlite3.Connection, tmp_path: Path) -> None:  # noqa: F811
    _check(conn)
    assert _matches_state(conn, "f4") == "absent"
    out = tmp_path / "review.toml"
    review.export_review(conn, out)
    _decide(out, "ok")
    assert review.apply_review(conn, out).applied == 1
    assert _matches_state(conn, "f4") == "present"
    # A new check keeps the decision.
    localtags.check_tags(conn, TmdbClient(FakeTransport(_routes()), "k", conn), recheck=True)
    assert _matches_state(conn, "f4") == "present"
    # Decided tags leave the next review file.
    assert review.export_review(conn, tmp_path / "again.toml").tags == 0


def test_ignore_and_bad_tag_decision(conn: sqlite3.Connection, tmp_path: Path) -> None:  # noqa: F811
    _check(conn)
    out = tmp_path / "review.toml"
    review.export_review(conn, out)
    _decide(out, "keep it")
    with pytest.raises(review.ReviewError, match="tag block 1"):
        review.apply_review(conn, out)
    review.export_review(conn, out, force=True)
    _decide(out, "ignore")
    review.apply_review(conn, out)
    result = matching.match(conn)
    assert result.flagged_tags == [] and _matches_state(conn, "f4") == "absent"


def _matches_state(conn: sqlite3.Connection, film: str) -> str:
    matching.match(conn)
    return _matches(conn)[film]["state"]
