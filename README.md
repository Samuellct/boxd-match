# boxd-match

Reads the official Letterboxd export (ZIP or folder), identifies every title by TMDB/IMDb id, keeps each list apart (watched, watchlist, likes, ratings, diary, custom lists) and matches titles against a local media library (tagged file names and Jellyfin).

Status: stages 1 to 5 work. Stage 6 (local matching) is not implemented yet.

## Setup

```bash
uv sync
uv run pytest                                  # never touches the network
cp boxd-match.example.toml boxd-match.toml     # list roles and database path, git-ignored
echo TMDB_API_KEY=... > .env                   # TMDB v3 key, git-ignored
```

## Usage

```bash
# Offline summary straight from the export
uv run boxd-match summary path/to/export-folder-or.zip --config boxd-match.toml [--json]

# Identification, stored in boxd-match.sqlite (path from [paths] database or --db)
uv run boxd-match import path/to/export --config boxd-match.toml
uv run boxd-match seed path/to/letterboxd_ids.json --config boxd-match.toml   # optional legacy cache
uv run boxd-match resolve --config boxd-match.toml        # TMDB, then Letterboxd pages if needed
uv run boxd-match coverage --config boxd-match.toml [--all]

# Manual review of what could not be confirmed
uv run boxd-match review export --config boxd-match.toml  # writes review.toml
uv run boxd-match review apply review.toml --config boxd-match.toml
```

Every command takes `--json`.

- **`import`** rebuilds the export tables. Identities, caches and review decisions are kept.
- **`seed`** loads an old `letterboxd_ids.json` cache as unchecked candidates. It never confirms anything.
- **`resolve`** checks candidates on TMDB first. It reads a Letterboxd film page only when TMDB cannot confirm the title, at most 1 request per second, with a `boxd-match/<version>` User-Agent. It stops with exit code 3 on a block (403, 503, challenge page, repeated 429) and resumes where it stopped when run again. `--tmdb-only` skips the page reads.
- **Statuses:** each film ends `confirmed`, `to_review` (with a reason) or `unresolved`. A title is confirmed only when a second source agrees: TMDB `/find` on the IMDb id, or title and year (±2) against TMDB.
- **`review export`** writes one block per film to review, with its Letterboxd link and the TMDB candidates. Fill `decision` with `movie/ID`, `tv/ID`, `tv/ID/season/N` (one season), `tv/ID/season/N/episode/M` (one episode; season 0 holds the specials) or `none`, then run `review apply`. A partly filled file is fine, and applying it again is safe.

## Pipeline

1. **Read the export.** CSV files and lists, plus `deleted/` and `orphaned/` kept apart.
2. **Build the film URI table.** Diary and review entries are attached to their film.
3. **Resolve each film URI once**, from the legacy cache or its Letterboxd page, with a permanent cache.
4. **Cross-check against TMDB.** Type, title and year must agree.
5. **Compute statuses and list membership.**
6. **Match local media** by `(type, tmdb_id)`, then by IMDb id. Not implemented yet.
