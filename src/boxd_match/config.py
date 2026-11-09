"""User configuration (TOML). List roles live here, never in code.

    [lists]
    "watchlist-dispo-en-local" = ["watchlist"]
    "collection-perso" = ["owned"]

    [paths]
    database = "boxd-match.sqlite"   # optional, this is the default

Keys of [lists] are list file names without `.csv`. Roles are free-form strings; only
`watchlist` has a meaning today (it feeds "to watch").
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

WATCHLIST_ROLE = "watchlist"
DEFAULT_DATABASE = "boxd-match.sqlite"


class ConfigError(Exception):
    pass


ROOT_KINDS = ("movies", "shows")


@dataclass(frozen=True)
class Root:
    """A local library folder, scanned read-only."""

    id: str
    path: str
    kind: str  # movies | shows
    resolution: str | None = None  # default when the file name has none
    optional: bool = False  # removable drive: absence is reported, not an error


@dataclass(frozen=True)
class Config:
    list_roles: dict[str, frozenset[str]] = field(default_factory=dict)
    database: str = DEFAULT_DATABASE
    roots: tuple[Root, ...] = ()

    def roles_for(self, slug: str) -> frozenset[str]:
        return self.list_roles.get(slug, frozenset())


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error

    lists = data.get("lists", {})
    if not isinstance(lists, dict):
        raise ConfigError(f"{path}: [lists] must be a table")
    roles: dict[str, frozenset[str]] = {}
    for slug, value in lists.items():
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f'{path}: lists."{slug}" must be a list of strings')
        roles[slug] = frozenset(value)

    paths = data.get("paths", {})
    database = paths.get("database", DEFAULT_DATABASE) if isinstance(paths, dict) else None
    if not isinstance(database, str) or not database:
        raise ConfigError(f"{path}: paths.database must be a non-empty string")
    return Config(list_roles=roles, database=database, roots=_roots(path, data.get("roots", [])))


def _roots(path: str | Path, raw: object) -> tuple[Root, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: roots must be an array of tables ([[roots]])")
    roots, seen = [], set()
    for n, item in enumerate(raw, 1):
        where = f"{path}: roots #{n}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a table")
        root_id, root_path, kind = item.get("id"), item.get("path"), item.get("kind")
        if not isinstance(root_id, str) or not root_id or root_id in seen:
            raise ConfigError(f"{where}: id must be a unique non-empty string")
        if not isinstance(root_path, str) or not root_path:
            raise ConfigError(f"{where}: path must be a non-empty string")
        if kind not in ROOT_KINDS:
            raise ConfigError(f"{where}: kind must be one of {', '.join(ROOT_KINDS)}")
        resolution = item.get("resolution")
        if resolution is not None and not isinstance(resolution, str):
            raise ConfigError(f"{where}: resolution must be a string")
        seen.add(root_id)
        roots.append(Root(root_id, root_path, kind, resolution, bool(item.get("optional", False))))
    return tuple(roots)
