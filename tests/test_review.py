from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest

from boxd_match import db, resolver, review
from boxd_match.cli import main
from boxd_match.config import Config
from boxd_match.export import read_export
from boxd_match.films import build_library
from test_letterboxd import PAGES, _tmdb
from fakes import FakeTransport


@pytest.fixture
def resolved(export_dir: Path, tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    path = tmp_path / "t.sqlite"
    conn = db.connect(path)
    db.import_library(conn, build_library(read_export(export_dir), Config()), "x")
    fetcher = resolver.PageFetcher(conn, FakeTransport(dict(PAGES)), sleep=lambda s: None)
    resolver.read_pages(conn, fetcher, _tmdb(conn))
    return conn, path


def _fill(path: Path, decisions: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    blocks = text.split("[[film]]")
    for i, block in enumerate(blocks):
        for uri, decision in decisions.items():
            if f'uri = "{uri}"' in block:
                blocks[i] = block.replace('decision = ""', f'decision = "{decision}"')
    path.write_text("[[film]]".join(blocks), encoding="utf-8")


def test_export_lists_films_to_review_with_candidates(resolved, tmp_path: Path) -> None:
    conn, _ = resolved
    out = tmp_path / "review.toml"
    assert review.export_review(conn, out).films == 4  # f5, f6 to_review; f7, f8 unresolved
    data = tomllib.loads(out.read_text(encoding="utf-8"))
    by_uri = {b["uri"]: b for b in data["film"]}
    crash = by_uri["https://boxd.it/f5"]
    assert crash["reason"] == "year_mismatch" and crash["decision"] == ""
    assert crash["candidates"] == ["movie/2  Crash (1996)  https://www.themoviedb.org/movie/2"]
    assert crash["letterboxd"] == "https://letterboxd.com/film/crash-2004-1/"


def test_export_refuses_to_overwrite(resolved, tmp_path: Path) -> None:
    conn, _ = resolved
    out = tmp_path / "review.toml"
    review.export_review(conn, out)
    with pytest.raises(review.ReviewError, match="already exists"):
        review.export_review(conn, out)
    review.export_review(conn, out, force=True)


def test_apply_partial_file_and_survives_reimport(resolved, export_dir: Path, tmp_path: Path) -> None:
    conn, _ = resolved
    out = tmp_path / "review.toml"
    review.export_review(conn, out)
    _fill(out, {"https://boxd.it/f5": "movie/2", "https://boxd.it/f8": "none"})
    assert (r := review.apply_review(conn, out)).applied == 2 and r.skipped == 2
    assert review.apply_review(conn, out).applied == 2  # idempotent
    db.import_library(conn, build_library(read_export(export_dir), Config()), "x")
    rows = {r["uri"]: r for r in conn.execute("SELECT * FROM identity")}
    f5 = rows["https://boxd.it/f5"]
    assert (f5["status"], f5["source"], f5["method"], f5["tmdb_id"]) == ("confirmed", "manual", "manual", "2")
    assert (rows["https://boxd.it/f8"]["status"], rows["https://boxd.it/f8"]["reason"]) == ("unresolved", "manual_none")
    # Decided films leave the next review file; resolve does not touch them.
    out2 = tmp_path / "review2.toml"
    assert review.export_review(conn, out2).films == 2
    assert resolver.non_final(conn) == 0


def test_invalid_decision_applies_nothing(resolved, tmp_path: Path) -> None:
    conn, _ = resolved
    out = tmp_path / "review.toml"
    review.export_review(conn, out)
    _fill(out, {"https://boxd.it/f5": "movie/2", "https://boxd.it/f6": "film 348"})
    with pytest.raises(review.ReviewError, match=r"block \d+ \(https://boxd.it/f6\)"):
        review.apply_review(conn, out)
    assert conn.execute("SELECT COUNT(*) FROM review_decision").fetchone()[0] == 0


def test_cli_review_round_trip(resolved, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn, path = resolved
    conn.close()
    out = tmp_path / "r.toml"
    assert main(["review", "export", "--db", str(path), "--output", str(out)]) == 0
    _fill(out, {"https://boxd.it/f6": "movie/348"})
    assert main(["review", "apply", str(out), "--db", str(path)]) == 0
    assert "applied 1, skipped (empty decision) 3" in capsys.readouterr().out
    assert main(["review", "export", "--db", str(path), "--output", str(out)]) == 2
    assert "already exists" in capsys.readouterr().err
