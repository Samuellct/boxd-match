"""Stage 1: read a Letterboxd export (directory or ZIP) into plain rows.

No interpretation happens here beyond parsing: joining and statuses live in
`films.py`. `profile.csv` is never read, so the e-mail it holds never enters
memory.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

Row = dict[str, str]

_ENCODING = "utf-8-sig"


class _Node(Protocol):
    """Common surface of `pathlib.Path` and `zipfile.Path`."""

    name: str

    def __truediv__(self, other: str) -> _Node: ...
    def exists(self) -> bool: ...
    def is_dir(self) -> bool: ...
    def iterdir(self): ...
    def open(self, mode: str = ..., *args, **kwargs): ...


@dataclass(frozen=True)
class ListEntry:
    position: int
    name: str
    year: str
    uri: str
    note: str


@dataclass(frozen=True)
class LetterboxdList:
    slug: str  # file name without `.csv`; the key used by configuration
    name: str
    date: str
    tags: tuple[str, ...]
    url: str
    description: str
    entries: tuple[ListEntry, ...]


@dataclass
class Export:
    source: str
    watched: list[Row] = field(default_factory=list)
    ratings: list[Row] = field(default_factory=list)
    watchlist: list[Row] = field(default_factory=list)
    liked_films: list[Row] = field(default_factory=list)
    diary: list[Row] = field(default_factory=list)
    reviews: list[Row] = field(default_factory=list)
    lists: list[LetterboxdList] = field(default_factory=list)
    # `orphaned/diary.csv` -> rows, same for `deleted/`.
    orphaned: dict[str, list[Row]] = field(default_factory=dict)
    deleted: dict[str, list[Row]] = field(default_factory=dict)


class ExportError(Exception):
    pass


def split_tags(value: str) -> tuple[str, ...]:
    return tuple(t.strip() for t in value.split(",") if t.strip())


def _read_text(node: _Node) -> str:
    with node.open("rb") as handle:
        return handle.read().decode(_ENCODING)


def _rows(node: _Node) -> list[Row]:
    # newline="" semantics: let the csv module handle CRLF and newlines in quoted cells.
    return list(csv.DictReader(io.StringIO(_read_text(node), newline="")))


def parse_list(slug: str, text: str) -> LetterboxdList:
    """Parse a `lists/*.csv` file.

    Layout: a `Letterboxd list export v7` line, a metadata header and row, then
    the `Position,...` header and the entries. The description is a quoted cell
    that may contain blank lines, so the file is never split on blank lines.
    """
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or not rows[0] or not rows[0][0].startswith("Letterboxd list export"):
        raise ExportError(f"lists/{slug}.csv: missing 'Letterboxd list export' line")
    try:
        start = next(i for i, row in enumerate(rows) if row and row[0] == "Position")
    except StopIteration:
        raise ExportError(f"lists/{slug}.csv: no 'Position' header row") from None

    meta: Row = {}
    for i in range(1, start):
        if rows[i] and rows[i][0] == "Date" and i + 1 < start:
            meta = dict(zip(rows[i], rows[i + 1]))
            break

    header = rows[start]
    entries = []
    for row in rows[start + 1 :]:
        if not row or not row[0].strip().isdigit():
            continue
        cells = dict(zip(header, row))
        entries.append(
            ListEntry(
                position=int(cells["Position"]),
                name=cells.get("Name", ""),
                year=cells.get("Year", "").strip(),
                uri=cells.get("URL", "").strip(),
                note=cells.get("Description", ""),
            )
        )
    return LetterboxdList(
        slug=slug,
        name=meta.get("Name", slug),
        date=meta.get("Date", ""),
        tags=split_tags(meta.get("Tags", "")),
        url=meta.get("URL", ""),
        description=meta.get("Description", ""),
        entries=tuple(entries),
    )


def _find_root(top: _Node) -> _Node:
    """Return the folder holding `watched.csv`, at most two levels down."""
    level = [top]
    for _ in range(3):
        for node in level:
            if (node / "watched.csv").exists():
                return node
        level = [child for node in level for child in node.iterdir() if child.is_dir()]
    raise ExportError("no watched.csv found: is this a Letterboxd export?")


def _open_source(path: Path) -> _Node:
    if path.is_dir():
        return path
    if path.is_file() and zipfile.is_zipfile(path):
        return zipfile.Path(zipfile.ZipFile(path))
    raise ExportError(f"{path}: not a directory or a ZIP file")


def _child(root: _Node, relative: str) -> _Node | None:
    node = root
    for part in relative.split("/"):
        node = node / part
    return node if node.exists() else None


def read_export(path: str | Path) -> Export:
    root = _find_root(_open_source(Path(path)))
    export = Export(source=str(path))
    targets = {
        "watched.csv": export.watched,
        "ratings.csv": export.ratings,
        "watchlist.csv": export.watchlist,
        "likes/films.csv": export.liked_films,
        "diary.csv": export.diary,
        "reviews.csv": export.reviews,
    }
    for relative, bucket in targets.items():
        node = _child(root, relative)
        if node is not None:
            bucket.extend(_rows(node))

    lists_dir = _child(root, "lists")
    if lists_dir is not None:
        files = sorted((n for n in lists_dir.iterdir() if n.name.endswith(".csv")), key=lambda n: n.name)
        export.lists = [parse_list(n.name[: -len(".csv")], _read_text(n)) for n in files]

    for side, bucket in (("orphaned", export.orphaned), ("deleted", export.deleted)):
        folder = _child(root, side)
        if folder is None:
            continue
        for node in sorted(folder.iterdir(), key=lambda n: n.name):
            if node.name.endswith(".csv"):
                bucket[node.name] = _rows(node)
    return export
