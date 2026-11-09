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


@dataclass(frozen=True)
class Config:
    list_roles: dict[str, frozenset[str]] = field(default_factory=dict)
    database: str = DEFAULT_DATABASE

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
    return Config(list_roles=roles, database=database)
