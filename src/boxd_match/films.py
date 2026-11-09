"""Stages 2 and 5: film URI table, diary attachment, statuses and lists.

Every status is an independent flag on a film. Nothing here touches the
network: diary entries that cannot be attached by exact (Name, Year) are left
`pending_redirect` for stage 3, which follows the entry URI redirect.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from boxd_match.config import WATCHLIST_ROLE, Config
from boxd_match.export import Export, LetterboxdList, Row, split_tags

ATTACHED = "attached"
PENDING_REDIRECT = "pending_redirect"


@dataclass
class Viewing:
    entry_uri: str
    logged: str  # `Date`: when the row was logged
    watched_on: str  # `Watched Date`: when the film was seen
    rating: float | None
    rewatch: bool
    tags: tuple[str, ...]
    review: str | None = None


@dataclass(frozen=True)
class ListMembership:
    slug: str
    position: int
    note: str


@dataclass
class Film:
    uri: str
    name: str
    year: str
    watched: bool = False
    on_watchlist: bool = False  # from watchlist.csv only
    liked: bool = False
    rating: float | None = None
    viewings: list[Viewing] = field(default_factory=list)
    lists: list[ListMembership] = field(default_factory=list)
    roles: set[str] = field(default_factory=set)  # from configured list roles

    @property
    def wanted(self) -> bool:
        """On the watchlist, directly or through a list with the watchlist role."""
        return self.on_watchlist or WATCHLIST_ROLE in self.roles

    @property
    def to_watch(self) -> bool:
        return self.wanted and not self.watched


@dataclass(frozen=True)
class EntryLink:
    """Outcome of attaching one diary or review row to a film."""

    source: str  # "diary.csv" / "reviews.csv"
    entry_uri: str
    name: str
    year: str
    status: str  # ATTACHED or PENDING_REDIRECT
    film_uri: str | None
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class OrphanSuggestion:
    """An orphaned row has no URI: title + year only ever yields a suggestion."""

    source: str
    name: str
    year: str
    candidates: tuple[str, ...]


@dataclass
class Library:
    films: dict[str, Film]
    entry_links: list[EntryLink]
    orphans: list[OrphanSuggestion]
    lists: dict[str, LetterboxdList]
    list_roles: dict[str, frozenset[str]]
    unknown_configured_lists: list[str]
    deleted_rows: int


def _key(name: str, year: str) -> tuple[str, str]:
    return (name, year.strip())


def _rating(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def build_library(export: Export, config: Config) -> Library:
    films: dict[str, Film] = {}

    def film_for(uri: str, name: str, year: str) -> Film:
        film = films.get(uri)
        if film is None:
            film = films[uri] = Film(uri=uri, name=name, year=year.strip())
        return film

    def from_rows(rows: list[Row]):
        for row in rows:
            uri = row["Letterboxd URI"].strip()
            if uri:
                yield row, film_for(uri, row["Name"], row["Year"])

    # Stage 2: film URI table.
    for _, film in from_rows(export.watched):
        film.watched = True
    for row, film in from_rows(export.ratings):
        film.rating = _rating(row.get("Rating", ""))
    for _, film in from_rows(export.watchlist):
        film.on_watchlist = True
    for _, film in from_rows(export.liked_films):
        film.liked = True
    for lst in export.lists:
        roles = config.roles_for(lst.slug)
        for entry in lst.entries:
            if not entry.uri:
                continue
            film = film_for(entry.uri, entry.name, entry.year)
            film.lists.append(ListMembership(lst.slug, entry.position, entry.note))
            film.roles |= roles

    by_title: dict[tuple[str, str], set[str]] = defaultdict(set)
    for film in films.values():
        by_title[_key(film.name, film.year)].add(film.uri)

    def candidates(name: str, year: str) -> tuple[str, ...]:
        return tuple(sorted(by_title.get(_key(name, year), ())))

    # Diary attachment: exact (Name, Year), single candidate only.
    links: list[EntryLink] = []
    viewing_by_entry: dict[str, Viewing] = {}
    diary_links: dict[str, EntryLink] = {}

    def attach(source: str, row: Row) -> Viewing | None:
        entry_uri = row["Letterboxd URI"].strip()
        found = candidates(row["Name"], row["Year"])
        film_uri = found[0] if len(found) == 1 else None
        links.append(
            EntryLink(
                source=source,
                entry_uri=entry_uri,
                name=row["Name"],
                year=row["Year"].strip(),
                status=ATTACHED if film_uri else PENDING_REDIRECT,
                film_uri=film_uri,
                candidates=found,
            )
        )
        viewing = Viewing(
            entry_uri=entry_uri,
            logged=row.get("Date", ""),
            watched_on=row.get("Watched Date", ""),
            rating=_rating(row.get("Rating", "")),
            rewatch=row.get("Rewatch", "").strip() == "Yes",
            tags=split_tags(row.get("Tags", "")),
            review=row.get("Review"),
        )
        if film_uri:
            films[film_uri].viewings.append(viewing)
        return viewing

    for row in export.diary:
        viewing = attach("diary.csv", row)
        viewing_by_entry[viewing.entry_uri] = viewing
        diary_links[viewing.entry_uri] = links[-1]

    # A review shares its entry URI with its diary row: merge the text there.
    for row in export.reviews:
        entry_uri = row["Letterboxd URI"].strip()
        same_entry = viewing_by_entry.get(entry_uri)
        if same_entry is not None:
            same_entry.review = row.get("Review", "")
            diary_link = diary_links[entry_uri]
            links.append(
                EntryLink(
                    source="reviews.csv",
                    entry_uri=entry_uri,
                    name=row["Name"],
                    year=row["Year"].strip(),
                    status=diary_link.status,
                    film_uri=diary_link.film_uri,
                    candidates=diary_link.candidates,
                )
            )
        else:
            attach("reviews.csv", row)

    orphans = [
        OrphanSuggestion(f"orphaned/{name}", row["Name"], row["Year"].strip(), candidates(row["Name"], row["Year"]))
        for name, rows in export.orphaned.items()
        for row in rows
        if "Name" in row and "Year" in row
    ]

    slugs = {lst.slug for lst in export.lists}
    return Library(
        films=films,
        entry_links=links,
        orphans=orphans,
        lists={lst.slug: lst for lst in export.lists},
        list_roles={slug: config.roles_for(slug) for slug in slugs},
        unknown_configured_lists=sorted(set(config.list_roles) - slugs),
        deleted_rows=sum(len(rows) for rows in export.deleted.values()),
    )


def summarize(library: Library) -> dict:
    films = list(library.films.values())

    def entry_stats(source: str) -> dict:
        own = [link for link in library.entry_links if link.source == source]
        attached = sum(link.status == ATTACHED for link in own)
        return {
            "rows": len(own),
            "attached": attached,
            "pending_redirect": [
                {"entry_uri": l.entry_uri, "name": l.name, "year": l.year, "candidates": list(l.candidates)}
                for l in own
                if l.status == PENDING_REDIRECT
            ],
        }

    def brief(film: Film) -> dict:
        return {"uri": film.uri, "name": film.name, "year": film.year}

    lists = []
    for slug, lst in library.lists.items():
        roles = sorted(library.list_roles.get(slug, ()))
        lists.append({"slug": slug, "name": lst.name, "tags": list(lst.tags), "entries": len(lst.entries), "roles": roles})

    watched_and_csv = sorted((f for f in films if f.watched and f.on_watchlist), key=lambda f: f.name)
    return {
        "films": len(films),
        "watched": sum(f.watched for f in films),
        "rated": sum(f.rating is not None for f in films),
        "liked": sum(f.liked for f in films),
        "watchlist_csv": sum(f.on_watchlist for f in films),
        "watchlist_with_roles": sum(f.wanted for f in films),
        "watched_and_watchlist_csv": [brief(f) for f in watched_and_csv],
        "watched_and_watchlist_with_roles": sum(f.watched and f.wanted for f in films),
        "to_watch": sum(f.to_watch for f in films),
        "viewings": sum(len(f.viewings) for f in films),
        "rewatches": sum(v.rewatch for f in films for v in f.viewings),
        "diary": entry_stats("diary.csv"),
        "reviews": entry_stats("reviews.csv"),
        "lists": lists,
        "list_entries": sum(item["entries"] for item in lists),
        "unknown_configured_lists": library.unknown_configured_lists,
        "orphans": [
            {"source": o.source, "name": o.name, "year": o.year, "suggestions": list(o.candidates)}
            for o in library.orphans
        ],
        "deleted_rows": library.deleted_rows,
    }
