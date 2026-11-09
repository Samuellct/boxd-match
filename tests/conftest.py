"""Synthetic Letterboxd export mimicking the real format.

Files are written as bytes with CRLF line endings (as Letterboxd ships them),
so git's line-ending conversion can never change what the tests exercise.

Content, by film URI:
  f1 Dune (2021)          watched, rated, liked, 2 diary entries (one rewatch, one review)
  f2 Heat (1995)          watched + watchlist.csv + list watchlist-dispo-en-local
  f4 Crash (2004)         watched        } same (Name, Year): the diary entry
  f5 Crash (2004)         watchlist.csv  } for "Crash" stays pending_redirect
  f6 Alien (1979)         watchlist.csv + watchlist-dispo-en-local
  f7 Stalker (1979)       collection-perso only
  f8 Amélie (2001)        watched + watchlist-dispo-en-local (watched after the move)
  f9 Paris, Texas (1984)  watchlist-dispo-en-local only
"""

from __future__ import annotations

from pathlib import Path

import pytest

FILES: dict[str, str] = {
    "profile.csv": """Date Joined,Username,Given Name,Family Name,Email Address,Location
2023-05-02,tester,,,secret@example.com,france
""",
    "watched.csv": """Date,Name,Year,Letterboxd URI
2023-05-02,Dune,2021,https://boxd.it/f1
2023-05-02,Heat,1995,https://boxd.it/f2
2023-06-01,Crash,2004,https://boxd.it/f4
2024-02-10,Amélie,2001,https://boxd.it/f8
""",
    "ratings.csv": """Date,Name,Year,Letterboxd URI,Rating
2024-11-24,Dune,2021,https://boxd.it/f1,4.5
""",
    "watchlist.csv": """Date,Name,Year,Letterboxd URI
2023-07-04,Heat,1995,https://boxd.it/f2
2023-07-05,Alien,1979,https://boxd.it/f6
2023-07-06,Crash,2004,https://boxd.it/f5
""",
    "likes/films.csv": """Date,Name,Year,Letterboxd URI
2023-05-02,Dune,2021,https://boxd.it/f1
""",
    # Entry URIs (e1, e2, e3) never match a film URI.
    "diary.csv": """Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date
2024-01-09,Dune,2021,https://boxd.it/e1,5,,"cinema, imax",2021-09-24
2024-11-24,Dune,2021,https://boxd.it/e2,4,Yes,,2024-11-20
2023-06-01,Crash,2004,https://boxd.it/e3,3,,,2023-05-30
""",
    "reviews.csv": """Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date
2024-01-09,Dune,2021,https://boxd.it/e1,5,,"Line one.

Line two, after a blank line.","cinema, imax",2021-09-24
""",
    "lists/watchlist-dispo-en-local.csv": """Letterboxd list export v7
Date,Name,Tags,URL,Description
2025-11-02,Watchlist dispo en local,,https://boxd.it/L1,"Moved here once available locally.

Position,Name,Year,URL,Description
1,Not,1900,https://boxd.it/fake,this line is inside the quoted description"

Position,Name,Year,URL,Description
1,Alien,1979,https://boxd.it/f6,
2,Heat,1995,https://boxd.it/f2,
3,Amélie,2001,https://boxd.it/f8,
4,"Paris, Texas",1984,https://boxd.it/f9,"A note

on two paragraphs"
""",
    "lists/collection-perso.csv": """Letterboxd list export v7
Date,Name,Tags,URL,Description
2024-03-01,Collection perso,"own, owned",https://boxd.it/L2,

Position,Name,Year,URL,Description
1,Stalker,1979,https://boxd.it/f7,
""",
    "orphaned/diary.csv": """Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date
2024-11-06,Heat,1995,,,,,2024-09-05
2024-11-19,"ZEN : \"\"La dernière\"\"\",2024,,5,,home,2024-11-17
""",
    "deleted/diary.csv": """Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date
""",
}

# Files saved with a UTF-8 byte-order mark, to exercise `utf-8-sig`.
WITH_BOM = {"watched.csv"}

CONFIG = """[lists]
"watchlist-dispo-en-local" = ["watchlist"]
"collection-perso" = ["owned"]
"""


def write_export(root: Path) -> Path:
    for relative, text in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.replace("\n", "\r\n").encode("utf-8")
        if relative in WITH_BOM:
            data = b"\xef\xbb\xbf" + data
        path.write_bytes(data)
    return root


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    return write_export(tmp_path / "letterboxd-export")


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "boxd-match.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path
