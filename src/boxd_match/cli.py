"""Command-line entry point. Subcommands are added as pipeline stages land."""

from __future__ import annotations

import argparse
import json
import sys

from boxd_match import __version__
from boxd_match.config import ConfigError, load_config
from boxd_match.export import ExportError, read_export
from boxd_match.films import build_library, summarize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boxd-match",
        description="Identify Letterboxd export titles and match them to local media.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    summary = commands.add_parser("summary", help="read an export offline and print its statuses")
    summary.add_argument("export", help="export folder or ZIP file")
    summary.add_argument("--config", help="TOML file with list roles ([lists] section)")
    summary.add_argument("--json", action="store_true", help="print the summary as JSON")
    return parser


def _print_text(s: dict) -> None:
    diary, reviews = s["diary"], s["reviews"]
    lines = [
        f"films (distinct URIs)        {s['films']}",
        f"watched                      {s['watched']}",
        f"rated                        {s['rated']}",
        f"liked                        {s['liked']}",
        f"watchlist.csv                {s['watchlist_csv']}",
        f"watchlist incl. list roles   {s['watchlist_with_roles']}",
        f"watched and in watchlist.csv {len(s['watched_and_watchlist_csv'])}",
        f"watched and wanted via roles {s['watched_and_watchlist_with_roles']}",
        f"to watch                     {s['to_watch']}",
        f"diary attached               {diary['attached']}/{diary['rows']}",
        f"reviews attached             {reviews['attached']}/{reviews['rows']}",
        f"viewings (rewatches)         {s['viewings']} ({s['rewatches']})",
        f"lists (entries)              {len(s['lists'])} ({s['list_entries']})",
        f"orphaned rows                {len(s['orphans'])}",
        f"deleted rows                 {s['deleted_rows']}",
    ]
    for film in s["watched_and_watchlist_csv"]:
        lines.append(f"  watched + watchlist.csv: {film['name']} ({film['year']}) {film['uri']}")
    for item in s["lists"]:
        if item["roles"]:
            lines.append(f"  list roles: {item['slug']} -> {', '.join(item['roles'])}")
    for source in (diary, reviews):
        for link in source["pending_redirect"]:
            lines.append(
                f"  pending redirect: {link['name']} ({link['year']}) {link['entry_uri']}"
                f" candidates={len(link['candidates'])}"
            )
    for orphan in s["orphans"]:
        hint = ", ".join(orphan["suggestions"]) or "no suggestion"
        lines.append(f"  orphan {orphan['source']}: {orphan['name']} ({orphan['year']}) -> {hint}")
    for slug in s["unknown_configured_lists"]:
        lines.append(f"  warning: configured list '{slug}' is not in the export")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "summary":
        parser.print_help()
        return 0
    try:
        config = load_config(args.config)
        summary = summarize(build_library(read_export(args.export), config))
    except (ConfigError, ExportError) as error:
        print(f"boxd-match: error: {error}", file=sys.stderr)
        return 2
    # Titles are not ASCII-only; never crash on a narrow Windows console codepage.
    if hasattr(sys.stdout, "reconfigure"):
        if args.json:
            sys.stdout.reconfigure(encoding="utf-8")
        else:
            sys.stdout.reconfigure(errors="backslashreplace")
    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
