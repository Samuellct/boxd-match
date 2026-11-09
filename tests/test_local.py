from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from boxd_match import db, local
from boxd_match.cli import main
from boxd_match.config import ConfigError, load_config


def _files(conn) -> dict[str, dict]:
    return {Path(r["path"]).name: dict(r) for r in conn.execute("SELECT * FROM local_file")}


def test_config_roots(library_config_file: Path) -> None:
    config = load_config(library_config_file)
    assert [r.id for r in config.roots] == ["M", "U", "S", "H"]
    assert config.roots[3].optional and config.roots[2].kind == "shows"


@pytest.mark.parametrize("toml", [
    '[[roots]]\nid = "A"\npath = "x"\nkind = "music"\n',
    '[[roots]]\nid = "A"\npath = "x"\nkind = "movies"\n[[roots]]\nid = "A"\npath = "y"\nkind = "shows"\n',
    '[[roots]]\nid = "A"\nkind = "movies"\n',
])
def test_bad_roots(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "c.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_scan_parses_items(library_config_file: Path, tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.sqlite")
    statuses = local.scan(conn, load_config(library_config_file).roots)
    assert [(s.id, s.present, s.files) for s in statuses] == [("M", True, 5), ("U", True, 1), ("S", True, 5), ("H", False, 0)]
    files = _files(conn)
    assert "notes.txt" not in files
    dune = files["Dune (2021) [imdbid-tt1160419] - 1080p - FR.mkv"]
    assert (dune["tag_key"], dune["title"], dune["year"], dune["res"]) == ("imdb:tt1160419", "Dune", 2021, "1080p")
    uhd = files["Dune (2021) [imdbid-tt1160419] - 2160p - FR - HDR.mkv"]
    # suivi-medias rule: 3-5 capitals are flags, anything else is the edition.
    assert (uhd["root"], uhd["res"], uhd["tag_key"], uhd["flags"], uhd["edition"]) == (
        "U", "2160p", "imdb:tt1160419", "HDR", "FR")
    heat = files["Heat (1995) [tmdbid-949].mkv"]
    assert (heat["tag_key"], heat["title"], heat["year"], heat["res"]) == ("tmdb:949", "Heat", 1995, "1080p")
    untagged = files["Untagged Movie (2001).mkv"]
    assert (untagged["tag_key"], untagged["title"], untagged["year"]) == (None, "Untagged Movie", 2001)
    ep = files["Twin Peaks S03E02.mkv"]
    assert (ep["kind"], ep["tag_key"], ep["season"], ep["episode"]) == ("shows", "tmdb:1920", 3, 2)
    assert files["Twin Peaks S00E01.mkv"]["season"] == 0


def test_absent_root_keeps_last_files(library_config_file: Path, tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.sqlite")
    roots = load_config(library_config_file).roots
    local.scan(conn, roots)
    shutil.rmtree(tmp_path / "library" / "uhd")
    statuses = {s.id: s for s in local.scan(conn, roots)}
    assert (statuses["U"].present, statuses["U"].files) == (False, 1)
    row = conn.execute("SELECT available FROM local_file WHERE root = 'U'").fetchone()
    assert row[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM local_file WHERE root = 'M' AND available = 1").fetchone()[0] == 5


def test_rescan_replaces_and_drops_removed_roots(library_config_file: Path, tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.sqlite")
    roots = load_config(library_config_file).roots
    local.scan(conn, roots)
    (tmp_path / "library" / "movies" / "Heat (1995) [tmdbid-949].mkv").unlink()
    local.scan(conn, roots[1:])  # root M removed from the config, Heat deleted
    assert conn.execute("SELECT COUNT(*) FROM local_file WHERE root = 'M'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM local_root WHERE id = 'M'").fetchone()[0] == 0


def test_cli_scan(library_config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--config", str(library_config_file), "--db", str(tmp_path / "t.sqlite"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["id"]: r["files"] for r in out["roots"]} == {"M": 5, "U": 1, "S": 5, "H": 0}


def test_cli_scan_without_roots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--db", str(tmp_path / "t.sqlite")]) == 2
    assert "[[roots]]" in capsys.readouterr().err
