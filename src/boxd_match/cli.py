"""Command-line entry point. Subcommands are added as pipeline stages land."""

from __future__ import annotations

import argparse
import json
import sys

from boxd_match import __version__
from boxd_match import db, resolver
from boxd_match.config import Config, ConfigError, load_config
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

    imp = commands.add_parser("import", help="load an export into the database (no network)")
    imp.add_argument("export", help="export folder or ZIP file")
    _db_options(imp)

    seed = commands.add_parser("seed", help="load letterboxd_ids.json as unchecked candidates (no network)")
    seed.add_argument("cache", help="path to letterboxd_ids.json")
    _db_options(seed)

    res = commands.add_parser("resolve", help="check identities on TMDB, read Letterboxd pages when needed")
    res.add_argument("--tmdb-only", action="store_true", help="only check legacy candidates on TMDB")
    _db_options(res)

    cov = commands.add_parser("coverage", help="identity statuses and reasons (no network)")
    cov.add_argument("--all", action="store_true", help="list every non-confirmed film, not only 30")
    _db_options(cov)

    rev = commands.add_parser("review", help="manual review through a TOML file (no network)")
    actions = rev.add_subparsers(dest="review_action", required=True)
    rexp = actions.add_parser("export", help="write films to review into a TOML file")
    rexp.add_argument("--output", default="review.toml", help="file to write (default: review.toml)")
    rexp.add_argument("--force", action="store_true", help="overwrite an existing file")
    _db_options(rexp)
    rapp = actions.add_parser("apply", help="record the decisions of a filled review file")
    rapp.add_argument("file", help="review file to apply")
    _db_options(rapp)
    return parser


def _db_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--config", help="TOML file with list roles and [paths] database")
    command.add_argument("--db", help="SQLite database path (overrides the config)")
    command.add_argument("--json", action="store_true", help="print the result as JSON")


def _open_db(args: argparse.Namespace, config: Config):
    return db.connect(args.db or config.database)


def _emit(args: argparse.Namespace, data: dict, lines: list[str]) -> None:
    if args.json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print("\n".join(lines))


def _cmd_import(args: argparse.Namespace, config: Config) -> int:
    library = build_library(read_export(args.export), config)
    conn = _open_db(args, config)
    db.import_library(conn, library, str(args.export))
    resolver.attach_redirects(conn)  # cached redirects only, no network
    c = db.export_counts(conn)
    _emit(args, c, [
        f"films                        {c['films']}",
        f"watched                      {c['watched']}",
        f"watched and in watchlist.csv {c['watched_and_watchlist_csv']}",
        f"to watch                     {c['to_watch']}",
        f"diary attached               {c['diary_attached']}/{c['diary_rows']}",
        f"reviews attached             {c['reviews_attached']}/{c['reviews_rows']}",
        f"lists (entries)              {c['lists']} ({c['list_entries']})",
        f"orphaned rows                {c['orphans']}",
    ])
    return 0


def _cmd_seed(args: argparse.Namespace, config: Config) -> int:
    r = resolver.seed_legacy(_open_db(args, config), args.cache)
    data = {"seeded": r.seeded, "not_in_export": r.not_in_export, "kept": r.kept, "errors": r.errors}
    _emit(args, data, [f"{k:<14}{v}" for k, v in data.items()])
    return 0


def _cmd_resolve(args: argparse.Namespace, config: Config) -> int:
    from boxd_match import net, tmdb

    conn = _open_db(args, config)
    try:
        client = tmdb.TmdbClient(net.tmdb_transport(), tmdb.load_api_key(), conn)
        progress = resolver.check_legacy(conn, client, resolver.stderr_report)
        fetcher = None
        if not args.tmdb_only:
            fetcher = resolver.PageFetcher(conn, net.letterboxd_transport())
            pages = resolver.read_pages(conn, fetcher, client, resolver.stderr_report)
            for key, value in pages.counts.items():
                progress.counts[key] = progress.counts.get(key, 0) + value
            progress.done += pages.done
            resolver.attach_redirects(conn, fetcher)
    except (tmdb.TmdbError, net.NetworkError, net.ForbiddenHost) as error:
        raise resolver.ResolveError(str(error)) from None
    data = {
        "processed": progress.done, "outcomes": progress.counts, "tmdb_requests": client.requests,
        "letterboxd_requests": fetcher.requests if fetcher else 0, "remaining": resolver.non_final(conn),
    }
    lines = [f"processed            {progress.done}", f"tmdb requests        {client.requests}",
             f"letterboxd requests  {data['letterboxd_requests']}"]
    lines += [f"  {k:<32}{v}" for k, v in sorted(progress.counts.items())]
    lines.append(f"remaining (pending or needs_page) {data['remaining']}")
    _emit(args, data, lines)
    return 0


def _cmd_coverage(args: argparse.Namespace, config: Config) -> int:
    conn = _open_db(args, config)
    counts = db.identity_counts(conn)
    rows = conn.execute(
        "SELECT f.name, f.year, i.uri, i.status, i.reason, i.tmdb_type, i.tmdb_id, i.tmdb_season,"
        " i.tmdb_episode FROM identity i"
        " JOIN film f ON f.uri = i.uri WHERE i.status != 'confirmed' ORDER BY i.status, i.reason, f.name"
    ).fetchall()
    data = {**counts, "films": [dict(r) for r in rows]}
    lines = ["status"] + [f"  {k:<14}{v}" for k, v in sorted(counts["by_status"].items())]
    lines += ["confirmed by method"] + [f"  {k:<14}{v}" for k, v in counts["confirmed_by_method"].items()]
    lines += ["confirmed by type"] + [f"  {k:<14}{v}" for k, v in sorted(counts["confirmed_by_type"].items())]
    lines += ["not confirmed, by reason"] + [f"  {k:<32}{v}" for k, v in counts["by_reason"].items()]
    shown = rows if args.all else rows[:30]
    for r in shown:
        candidate = f"{r['tmdb_type'] or '?'}/{r['tmdb_id']}" if r["tmdb_id"] else "-"
        lines.append(f"  {r['status']:<11}{(r['reason'] or ''):<20}{r['name']} ({r['year']}) {r['uri']} {candidate}")
    if len(rows) > len(shown):
        lines.append(f"  ... {len(rows) - len(shown)} more (--all)")
    _emit(args, data, lines)
    return 0


def _cmd_review(args: argparse.Namespace, config: Config) -> int:
    from boxd_match import review

    conn = _open_db(args, config)
    try:
        if args.review_action == "export":
            r = review.export_review(conn, args.output, force=args.force)
            _emit(args, {"file": str(r.path), "films": r.films}, [f"{r.films} films written to {r.path}"])
        else:
            r = review.apply_review(conn, args.file)
            _emit(args, {"applied": r.applied, "skipped": r.skipped},
                  [f"applied {r.applied}, skipped (empty decision) {r.skipped}"])
    except review.ReviewError as error:
        print(f"boxd-match: error: {error}", file=sys.stderr)
        return 2
    return 0


_DB_COMMANDS = {
    "import": _cmd_import, "seed": _cmd_seed, "resolve": _cmd_resolve,
    "coverage": _cmd_coverage, "review": _cmd_review,
}


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
    if args.command is None:
        parser.print_help()
        return 0
    # Titles are not ASCII-only; never crash on a narrow Windows console codepage.
    if hasattr(sys.stdout, "reconfigure"):
        if getattr(args, "json", False):
            sys.stdout.reconfigure(encoding="utf-8")
        else:
            sys.stdout.reconfigure(errors="backslashreplace")
    try:
        config = load_config(args.config)
        if args.command in _DB_COMMANDS:
            return _DB_COMMANDS[args.command](args, config)
        summary = summarize(build_library(read_export(args.export), config))
    except resolver.Blocked as error:
        print(f"boxd-match: stopped: {error}. Progress is saved; resume later with the same"
              " command. No workaround is attempted.", file=sys.stderr)
        return 3
    except resolver.ResolveError as error:
        print(f"boxd-match: error: {error}. Progress is saved; run the same command to resume.",
              file=sys.stderr)
        return 2
    except (ConfigError, ExportError, db.SchemaError) as error:
        print(f"boxd-match: error: {error}", file=sys.stderr)
        return 2
    if args.json:
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
