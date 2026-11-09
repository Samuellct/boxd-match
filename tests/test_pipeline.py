from __future__ import annotations

import ast
import json
import zipfile
from pathlib import Path

import pytest

from boxd_match.cli import main
from boxd_match.config import Config, ConfigError, load_config
from boxd_match.export import ExportError, parse_list, read_export
from boxd_match.films import ATTACHED, PENDING_REDIRECT, build_library, summarize


def _summary(export_dir: Path, config: Config | None = None) -> dict:
    return summarize(build_library(read_export(export_dir), config or Config()))


# Stage 1: reading


def test_raw_files_use_crlf(export_dir: Path) -> None:
    assert b"\r\n" in (export_dir / "diary.csv").read_bytes()


def test_reads_film_files_with_bom(export_dir: Path) -> None:
    export = read_export(export_dir)
    assert [r["Name"] for r in export.watched] == ["Dune", "Heat", "Crash", "Amélie"]
    assert "Date" in export.watched[0]  # BOM stripped from the first header


def test_list_with_multiline_description_and_fake_position_line(export_dir: Path) -> None:
    lst = next(l for l in read_export(export_dir).lists if l.slug == "watchlist-dispo-en-local")
    assert lst.name == "Watchlist dispo en local"
    assert lst.url == "https://boxd.it/L1"
    assert "\r\n\r\nPosition," in lst.description
    assert [(e.position, e.name, e.uri) for e in lst.entries] == [
        (1, "Alien", "https://boxd.it/f6"),
        (2, "Heat", "https://boxd.it/f2"),
        (3, "Amélie", "https://boxd.it/f8"),
        (4, "Paris, Texas", "https://boxd.it/f9"),
    ]
    assert "two paragraphs" in lst.entries[3].note


def test_list_tags_are_split(export_dir: Path) -> None:
    lst = next(l for l in read_export(export_dir).lists if l.slug == "collection-perso")
    assert lst.tags == ("own", "owned")


def test_list_without_position_header_is_an_error() -> None:
    with pytest.raises(ExportError, match="Position"):
        parse_list("broken", "Letterboxd list export v7\r\nDate,Name,Tags,URL,Description\r\n")


def test_multiline_review_survives(export_dir: Path) -> None:
    (row,) = read_export(export_dir).reviews
    assert row["Review"].startswith("Line one.")
    assert "Line two" in row["Review"]


def test_reads_zip_with_top_folder(export_dir: Path, tmp_path: Path) -> None:
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in export_dir.rglob("*.csv"):
            zf.write(path, f"letterboxd-user-2026/{path.relative_to(export_dir).as_posix()}")
    assert _summary(archive) == _summary(export_dir)


def test_not_an_export(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="watched.csv"):
        read_export(tmp_path)


# Stage 2: film table and diary attachment


def test_film_table_is_union_of_film_uris(export_dir: Path) -> None:
    films = build_library(read_export(export_dir), Config()).films
    assert sorted(films) == [f"https://boxd.it/f{n}" for n in (1, 2, 4, 5, 6, 7, 8, 9)]
    assert not any("/e" in uri for uri in films)  # entry URIs never become films


def test_diary_attaches_by_unique_title_year(export_dir: Path) -> None:
    library = build_library(read_export(export_dir), Config())
    links = {l.entry_uri: l for l in library.entry_links if l.source == "diary.csv"}
    assert links["https://boxd.it/e1"].status == ATTACHED
    assert links["https://boxd.it/e1"].film_uri == "https://boxd.it/f1"
    crash = links["https://boxd.it/e3"]
    assert crash.status == PENDING_REDIRECT
    assert crash.film_uri is None
    assert crash.candidates == ("https://boxd.it/f4", "https://boxd.it/f5")


def test_viewings_rewatch_tags_and_review(export_dir: Path) -> None:
    dune = build_library(read_export(export_dir), Config()).films["https://boxd.it/f1"]
    first, second = dune.viewings
    assert (first.logged, first.watched_on, first.tags) == ("2024-01-09", "2021-09-24", ("cinema", "imax"))
    assert first.rewatch is False and second.rewatch is True
    assert first.review is not None and "Line two" in first.review
    assert second.review is None


def test_orphans_only_get_suggestions(export_dir: Path) -> None:
    library = build_library(read_export(export_dir), Config())
    heat, zen = library.orphans
    assert heat.candidates == ("https://boxd.it/f2",)
    assert zen.name == 'ZEN : "La dernière"' and zen.candidates == ()
    # A suggestion never adds a viewing.
    assert library.films["https://boxd.it/f2"].viewings == []


# Stage 5: statuses and list roles


def test_statuses_are_independent_flags(export_dir: Path) -> None:
    s = _summary(export_dir)
    assert s["watched"] == 4
    assert s["watchlist_csv"] == 3
    assert [f["name"] for f in s["watched_and_watchlist_csv"]] == ["Heat"]
    assert (s["rated"], s["liked"]) == (1, 1)
    assert s["diary"]["attached"] == 2 and s["diary"]["rows"] == 3
    assert s["reviews"]["attached"] == 1 and s["reviews"]["rows"] == 1
    assert (s["viewings"], s["rewatches"]) == (2, 1)
    assert s["deleted_rows"] == 0


def test_without_roles_lists_do_not_feed_to_watch(export_dir: Path) -> None:
    s = _summary(export_dir)
    assert s["to_watch"] == 2  # Alien, Crash (f5)
    assert s["watched_and_watchlist_with_roles"] == 1


def test_watchlist_role_from_config(export_dir: Path, config_file: Path) -> None:
    s = _summary(export_dir, load_config(config_file))
    assert s["watchlist_with_roles"] == 5
    assert s["to_watch"] == 3  # + Paris, Texas, which is only in the list
    # watchlist.csv-only intersection is unchanged; the role adds Amélie.
    assert len(s["watched_and_watchlist_csv"]) == 1
    assert s["watched_and_watchlist_with_roles"] == 2
    roles = {item["slug"]: item["roles"] for item in s["lists"]}
    assert roles == {"collection-perso": ["owned"], "watchlist-dispo-en-local": ["watchlist"]}


def test_config_rejects_bad_roles(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[lists]\n"x" = "watchlist"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_configured_list_is_reported(export_dir: Path, tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('[lists]\n"typo-list" = ["watchlist"]\n', encoding="utf-8")
    assert _summary(export_dir, load_config(path))["unknown_configured_lists"] == ["typo-list"]


# CLI and guarantees


def test_cli_json_never_contains_the_email(
    export_dir: Path, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["summary", str(export_dir), "--config", str(config_file), "--json"]) == 0
    out = capsys.readouterr().out
    assert "secret@example.com" not in out
    assert json.loads(out)["to_watch"] == 3


def test_cli_text_output(export_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary", str(export_dir)]) == 0
    out = capsys.readouterr().out
    assert "diary attached               2/3" in out
    assert "pending redirect: Crash (2004) https://boxd.it/e3 candidates=2" in out


def test_cli_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary", str(tmp_path)]) == 2
    assert "watched.csv" in capsys.readouterr().err


def test_package_imports_no_network_module() -> None:
    forbidden = {"socket", "http", "urllib", "requests", "httpx", "ssl"}
    package = Path(__file__).parents[1] / "src" / "boxd_match"
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not {n.split(".")[0] for n in names} & forbidden, source.name
