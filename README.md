# boxd-match

Reads the official Letterboxd export (ZIP or folder), identifies every title by TMDB/IMDb id, keeps each list apart (watched, watchlist, likes, ratings, diary, custom lists) and matches titles against a local media library (tagged file names and Jellyfin).

Status: stages 1, 2 and 5 work offline (`summary` command). Stages 3, 4 and 6 are not implemented yet.

## Setup

```bash
uv sync
uv run pytest
uv run boxd-match --version
```

## Usage

```bash
cp boxd-match.example.toml boxd-match.toml   # list roles, git-ignored
uv run boxd-match summary path/to/export-folder-or.zip --config boxd-match.toml
uv run boxd-match summary path/to/export --config boxd-match.toml --json
```

`summary` reads the export without any network access and prints counts: films, watched, watchlist, "to watch", diary entries attached to their film, entries left for redirect resolution (stage 3), orphaned rows with title+year suggestions.

## Planned pipeline

1. **Read the export.** CSV files and lists, plus `deleted/` and `orphaned/` kept apart.
2. **Build the film URI table.** Diary and review entries are attached to their film.
3. **Resolve each film URI once** from its Letterboxd page, with a permanent cache.
4. **Cross-check against TMDB.** Type, title and year must agree.
5. **Compute statuses and list membership.**
6. **Match local media** by `(type, tmdb_id)`, then by IMDb id.

The background study (in French) lives outside this repository: `Audit_serveur/letterboxd-analyse/RAPPORT.md`.
