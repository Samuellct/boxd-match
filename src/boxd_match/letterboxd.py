"""Read a Letterboxd film page (pure parsing, no network).

Trusted: the outbound link `<a data-track-action="TMDB" href=".../(movie|tv)/ID/">`.
`<body data-tmdb-id>` is only compared with it: it is empty or wrong for series.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from boxd_match.net import Response

_A_TAG = re.compile(r"<a\b[^>]*>", re.I)
_TMDB_HREF = re.compile(r'href="https?://(?:www\.)?themoviedb\.org/(movie|tv)/(\d+)/?"')
_IMDB_HREF = re.compile(r'href="https?://(?:www\.)?imdb\.com/title/(tt\d+)')
_BODY_ATTR = re.compile(r'<body\b[^>]*\bdata-tmdb-id="(\d*)"', re.I)
_FILM_SLUG = re.compile(r"letterboxd\.com/(?:[^/]+/)?film/([^/?#]+)")


@dataclass(frozen=True)
class FilmPage:
    final_url: str
    slug: str | None
    link_type: str | None  # movie | tv, from the TMDB link
    link_id: str | None
    attr_id: str | None  # body data-tmdb-id, None when empty
    imdb_id: str | None

    @property
    def conflict(self) -> bool:
        return bool(self.link_id and self.attr_id and self.link_id != self.attr_id)


def slug_from_url(url: str | None) -> str | None:
    match = _FILM_SLUG.search(url or "")
    return match.group(1) if match else None


def _tracked_link(html: str, action: str, href: re.Pattern[str]) -> re.Match[str] | None:
    for tag in _A_TAG.finditer(html):
        text = tag.group(0)
        if f'data-track-action="{action}"' in text:
            found = href.search(text)
            if found:
                return found
    return None


def parse_film_page(final_url: str, html: str) -> FilmPage:
    link = _tracked_link(html, "TMDB", _TMDB_HREF)
    link_type, link_id = (link.group(1), link.group(2)) if link else (None, None)
    if link_id is not None and int(link_id) == 0:  # `tv/0` is a placeholder, not an id
        link_type, link_id = None, None
    imdb = _tracked_link(html, "IMDb", _IMDB_HREF) or _IMDB_HREF.search(html)
    attr = _BODY_ATTR.search(html)
    return FilmPage(
        final_url=final_url,
        slug=slug_from_url(final_url),
        link_type=link_type,
        link_id=link_id,
        attr_id=(attr.group(1) or None) if attr else None,
        imdb_id=imdb.group(1) if imdb else None,
    )


def is_challenge(response: Response) -> bool:
    """Cloudflare (or similar) interstitial instead of the page."""
    headers = {k.lower(): v for k, v in response.headers.items()}
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    head = response.body[:4000].decode("utf-8", "replace").lower()
    return "<title>just a moment" in head or "<title>attention required" in head
