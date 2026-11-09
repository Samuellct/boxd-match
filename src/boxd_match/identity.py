"""Pure decision logic: is a TMDB candidate confirmed by a second source?

A candidate is confirmed when either
- TMDB `/find` on the IMDb id returns the same (type, id), or
- the Letterboxd title matches a TMDB title (`title`, `original_title` or a
  translation; `name`/`original_name` for series) and the years differ by at
  most YEAR_TOLERANCE.
A `/find` answer that points elsewhere blocks confirmation. Candidates from the
legacy cache have no known type, so they need `/find` to confirm.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from boxd_match.db import CONFIRMED, TO_REVIEW

YEAR_TOLERANCE = 2

# Reason codes
TITLE_MISMATCH = "title_mismatch"
YEAR_MISMATCH = "year_mismatch"
TYPE_MISMATCH = "type_mismatch"
FIND_MISMATCH = "find_mismatch"
TYPE_UNVERIFIED = "type_unverified"
TMDB_NOT_FOUND = "tmdb_not_found"
LINK_ATTR_CONFLICT = "link_attr_conflict"
NO_TMDB_LINK = "no_tmdb_link"
NO_TMDB_ID = "no_tmdb_id"
PAGE_NOT_FOUND = "page_not_found"

# Confirmation methods
BY_FIND = "imdb_find"
BY_TITLE_YEAR = "title_year"
BY_MANUAL = "manual"


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str | None = None
    method: str | None = None
    imdb_id: str | None = None


def normalize(title: str) -> str:
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[^\W_]+", text))


def tmdb_titles(tmdb_type: str, details: dict) -> set[str]:
    keys = ("name", "original_name") if tmdb_type == "tv" else ("title", "original_title")
    titles = {details.get(k) or "" for k in keys}
    for tr in (details.get("translations") or {}).get("translations", []):
        data = tr.get("data") or {}
        titles.add(data.get("name") or data.get("title") or "")
    return {normalize(t) for t in titles if t}


def tmdb_year(tmdb_type: str, details: dict) -> int | None:
    date = details.get("first_air_date" if tmdb_type == "tv" else "release_date") or ""
    return int(date[:4]) if date[:4].isdigit() else None


def years_close(letterboxd_year: str, other: int | None) -> bool:
    return letterboxd_year.strip().isdigit() and other is not None and abs(int(letterboxd_year) - other) <= YEAR_TOLERANCE


def find_hits(find_body: dict | None) -> set[tuple[str, str]]:
    if not find_body:
        return set()
    hits = {("movie", str(r["id"])) for r in find_body.get("movie_results", [])}
    hits |= {("tv", str(r["id"])) for r in find_body.get("tv_results", [])}
    return hits


def check_candidate(
    *,
    name: str,
    year: str,
    tmdb_type: str,
    tmdb_id: str,
    details_status: int,
    details: dict | None,
    find_body: dict | None,
    imdb_id: str | None,
    require_find: bool,
) -> Verdict:
    """Decide on one candidate. `imdb_id` is the one known before TMDB (page or cache)."""
    if details_status == 404 or details is None:
        return Verdict(TO_REVIEW, TMDB_NOT_FOUND, imdb_id=imdb_id)
    imdb = imdb_id or (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id")
    hits = find_hits(find_body)
    if (tmdb_type, tmdb_id) in hits:
        return Verdict(CONFIRMED, method=BY_FIND, imdb_id=imdb)
    if hits:
        other_types = {t for t, _ in hits}
        reason = TYPE_MISMATCH if tmdb_type not in other_types else FIND_MISMATCH
        return Verdict(TO_REVIEW, reason, imdb_id=imdb)
    if require_find:
        return Verdict(TO_REVIEW, TYPE_UNVERIFIED, imdb_id=imdb)
    if normalize(name) not in tmdb_titles(tmdb_type, details):
        return Verdict(TO_REVIEW, TITLE_MISMATCH, imdb_id=imdb)
    if not years_close(year, tmdb_year(tmdb_type, details)):
        return Verdict(TO_REVIEW, YEAR_MISMATCH, imdb_id=imdb)
    return Verdict(CONFIRMED, method=BY_TITLE_YEAR, imdb_id=imdb)
