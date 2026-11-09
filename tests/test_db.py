from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from boxd_match import db
from boxd_match.cli import main
from boxd_match.config import Config, load_config
from boxd_match.export import read_export
from boxd_match.films import build_library, summarize


def _import(export_dir: Path, path: Path, config: Config | None = None) -> sqlite3.Connection:
    conn = db.connect(path)
    db.import_library(conn, build_library(read_export(export_dir), config or Config()), str(export_dir))
    return conn


def test_import_matches_summary(export_dir: Path, config_file: Path, tmp_path: Path) -> None:
    config = load_config(config_file)
    conn = _import(export_dir, tmp_path / "t.sqlite", config)
    counts = db.export_counts(conn)
    s = summarize(build_library(read_export(export_dir), config))
    assert counts["films"] == s["films"] == 8
    assert counts["watched"] == s["watched"]
    assert counts["watched_and_watchlist_csv"] == len(s["watched_and_watchlist_csv"])
    assert counts["to_watch"] == s["to_watch"] == 3
    assert (counts["diary_attached"], counts["diary_rows"]) == (2, 3)
    assert (counts["reviews_attached"], counts["reviews_rows"]) == (1, 1)
    assert counts["list_entries"] == s["list_entries"]


def test_pending_viewing_is_stored_without_film(export_dir: Path, tmp_path: Path) -> None:
    conn = _import(export_dir, tmp_path / "t.sqlite")
    row = conn.execute("SELECT film_uri FROM viewing WHERE entry_uri = 'https://boxd.it/e3'").fetchone()
    assert row is not None and row["film_uri"] is None
    review = conn.execute("SELECT review FROM viewing WHERE entry_uri = 'https://boxd.it/e1'").fetchone()
    assert "Line two" in review["review"]


def test_every_film_gets_a_pending_identity(export_dir: Path, tmp_path: Path) -> None:
    conn = _import(export_dir, tmp_path / "t.sqlite")
    rows = conn.execute("SELECT status, COUNT(*) FROM identity GROUP BY status").fetchall()
    assert [tuple(r) for r in rows] == [(db.PENDING, 8)]


def test_reimport_keeps_identities_caches_and_decisions(export_dir: Path, tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite"
    conn = _import(export_dir, path)
    conn.execute(
        "UPDATE identity SET status = 'confirmed', source = 'manual', tmdb_type = 'movie', tmdb_id = '1'"
        " WHERE uri = 'https://boxd.it/f1'"
    )
    conn.execute("INSERT INTO review_decision VALUES ('https://boxd.it/f1', 'movie/1', 'x')")
    conn.execute("INSERT INTO tmdb_cache VALUES ('movie/1', 200, '{}', 'x')")
    conn.commit()
    conn.close()
    conn = _import(export_dir, path)
    assert conn.execute("SELECT status FROM identity WHERE uri = 'https://boxd.it/f1'").fetchone()[0] == "confirmed"
    assert conn.execute("SELECT COUNT(*) FROM review_decision").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM film").fetchone()[0] == 8  # rebuilt, not doubled


def test_email_never_reaches_the_database(export_dir: Path, tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite"
    _import(export_dir, path).close()
    assert b"secret@example.com" not in path.read_bytes()


def test_schema_version_mismatch_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite"
    conn = db.connect(path)
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(db.SchemaError):
        db.connect(path)


def test_cli_import_json(export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import", str(export_dir), "--db", str(tmp_path / "c.sqlite"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["watched"] == 4 and out["diary_attached"] == 2


def test_database_path_from_config(export_dir: Path, tmp_path: Path) -> None:
    config = tmp_path / "c.toml"
    target = tmp_path / "from-config.sqlite"
    config.write_text(f'[paths]\ndatabase = "{target.as_posix()}"\n', encoding="utf-8")
    assert main(["import", str(export_dir), "--config", str(config)]) == 0
    assert target.exists()
