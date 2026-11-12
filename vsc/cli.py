"""Command-line front-end. Thin: parsing, progress and printing; the work lives in the library."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, pipeline, report
from .compress import FIRST, FIXED_FORMATS, FORMATS, MODES, PRESETS, Settings
from .probe import OK
from .project import Project, project_path
from .units import format_size, parse_size


def _progress(label: str):
    """Progress callback printing 'label: done/total' on one stderr line, at most twice a second."""
    last = [0.0]

    def cb(done: int, total: int | None = None) -> None:
        now = time.monotonic()
        if now - last[0] >= 0.5 or done == total:
            last[0] = now
            print(f"\r{label}: {done}" + (f"/{total}" if total else ""), end="", file=sys.stderr, flush=True)
            if done == total:
                print(file=sys.stderr)

    return cb


def cmd_scan(args: argparse.Namespace) -> int:
    threshold = parse_size(args.threshold)
    project = Project(args.project)

    state, result = pipeline.scan_project(
        project, args.paths, threshold, args.exclude, refresh=args.refresh,
        concurrency=args.concurrency, file_progress=_progress("Files"), url_progress=_progress("URLs"), log=print,
    )
    for f, err in result.errors[:10]:
        print(f"  ! {f}: {err}")
    if len(result.errors) > 10:
        print(f"  ! ... and {len(result.errors) - 10} more file errors (see state.json)")
    summary = report.scan_summary(state)

    print(f"\nThreshold {format_size(threshold)}:")
    print(f"  {summary['over_threshold']} sheets over threshold "
          f"({format_size(summary['over_threshold_bytes'])} of {format_size(summary['total_bytes'])} total)")
    for status, n in sorted(summary["by_status"].items()):
        print(f"  {status:>10}: {n}")
    sheets = report.by_size(state["sheets"])
    over = [s for s in sheets if report.over_threshold(s, threshold)][: args.top]
    if over:
        print(f"\nLargest {len(over)}:")
        for s in over:
            print(f"  {format_size(report.size_of(s)):>10}  {len(s['refs']):>4} refs  {report.label(s)}  {s['key']}")
    bad = [s for s in sheets if report.probe_status(s) not in (OK, "not-probed")]
    if bad:
        print(f"\nProblem URLs ({len(bad)}):")
        for s in bad[: args.top]:
            print(f"  {report.probe_text(s)}  {s['key']}")
    print(f"\nReports: {project.reports_dir.resolve()}")
    return 0


def cmd_compress(args: argparse.Namespace) -> int:
    project = Project(args.project)
    state = project.load_state()
    if not state.get("sheets"):
        print(f"No scan found in {project.root}. Run `vsc scan` first.", file=sys.stderr)
        return 2
    threshold = parse_size(args.threshold) if args.threshold else state["threshold"]
    formats = tuple(f.strip().lower() for f in args.formats.split(",") if f.strip())
    if bad := [f for f in formats if f not in FORMATS]:
        print(f"Unknown format(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    settings = Settings(
        target=args.target if args.target is not None else PRESETS[args.preset],
        mode=args.mode,
        formats=formats,
        fixed_format=args.fixed_format,
        fixed_quality=None if args.fixed_format == "jpeg-keep" else args.fixed_quality,
        min_saving=args.min_saving / 100,
        **({"workers": args.workers} if args.workers else {}),
    )

    sheets = pipeline.select(state, threshold, settings, args.include_unknown, args.match, args.force)
    sheets = sheets[: args.limit] if args.limit else sheets
    if not sheets:
        print("Nothing to compress (all sheets under the threshold or already done; --force to redo).")
        return 0
    print(f"Compressing {len(sheets)} sheets ({format_size(sum(report.size_of(s) or 0 for s in sheets))}): "
          f"{settings.describe()}")

    pipeline.run(project, state, sheets, settings, threshold,
                 auto_approve=not args.no_auto_approve, download_progress=_progress("Downloads"))
    pipeline.export(project, state, log=print)

    totals = report.compress_totals(sheets)
    print("\nDone:")
    for status, n in sorted(totals["counts"].items()):
        print(f"  {status:>14}: {n}")
    if totals["before"]:
        print(f"  compressed sheets: {format_size(totals['before'])} -> {format_size(totals['after'])} "
              f"(-{1 - totals['after'] / totals['before']:.0%})")
    print(f"\nFiles to upload: {project.out_dir.resolve()}")
    print(f"Report: {(project.reports_dir / 'final_report.csv').resolve()}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    from . import mapping

    project = Project(args.project)
    state = project.load_state()
    if not state.get("sheets"):
        print(f"No scan found in {project.root}. Run `vsc scan` first.", file=sys.stderr)
        return 2
    if args.urls:
        sources = [Path(args.urls).read_bytes()]
        print(f"Reading new URLs from {args.urls}")
    else:
        found = [Path(p) for p in args.cloudinfo] if args.cloudinfo else mapping.find_cloudinfo()
        if not found:
            print(f"TTS's upload list wasn't found. It's at {mapping.cloudinfo_hint()}; "
                  "pass it with --cloudinfo PATH, or new URLs with --urls FILE.", file=sys.stderr)
            return 2
        for p in found:
            print(f"Reading uploads from {p}")
        sources = [p.read_bytes() for p in found]

    mapping.map_uploads(project, state, sources, check=not args.no_check, log=print, progress=_progress("URLs"))
    if args.apply or args.dry_run:
        result = mapping.apply(project, state, dry_run=args.dry_run, log=print, progress=_progress("Files"))
        for e in result["errors"]:
            print(f"  ! {e}")
    print(f"Report: {(project.reports_dir / 'final_report.csv').resolve()}")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    from . import mapping

    project = Project(args.project)
    mapping.undo(project, project.load_state(), log=print)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vsc", description="Volumetric Sheet Compressor")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, help: str, func) -> argparse.ArgumentParser:
        c = sub.add_parser(name, help=help)
        c.add_argument("-p", "--project", default="default", type=project_path,
                       help="project name (stored in the tool's projects folder) or a folder path "
                            "(default: %(default)s)")
        c.set_defaults(func=func)
        return c

    s = command("scan", "find decksheets and check their sizes", cmd_scan)
    s.add_argument("paths", nargs="+", help="JSON files or folders")
    s.add_argument("-t", "--threshold", default="2MB", help="size threshold, e.g. 2MB, 500KB (default: %(default)s)")
    s.add_argument("-x", "--exclude", action="append", default=[], help="glob to exclude (repeatable)")
    s.add_argument("--refresh", action="store_true", help="re-check sizes even if cached")
    s.add_argument("-c", "--concurrency", type=int, default=8, help="parallel requests (default: %(default)s)")
    s.add_argument("--top", type=int, default=15, help="how many rows to print (default: %(default)s)")

    c = command("compress", "compress sheets over the threshold", cmd_compress)
    c.add_argument("-t", "--threshold", help="size threshold (default: the one used for the scan)")
    c.add_argument("--preset", choices=list(PRESETS), default="very-high",
                   help="quality preset: " + ", ".join(f"{k}={v:g}" for k, v in PRESETS.items())
                        + " (default: %(default)s)")
    c.add_argument("--target", type=float, help="explicit worst-tile SSIMULACRA2 target (overrides --preset)")
    c.add_argument("--mode", choices=MODES, default=FIRST,
                   help="first: formats in priority order, keep the first that passes; smallest: try all, keep "
                        "the smallest; fixed: one format/quality for everything (default: %(default)s)")
    c.add_argument("--formats", default=",".join(FORMATS),
                   help="allowed formats in priority order (default: %(default)s)")
    c.add_argument("--fixed-format", choices=FIXED_FORMATS, default="jpeg",
                   help="format for --mode fixed (default: %(default)s)")
    c.add_argument("--fixed-quality", type=int, default=85, help="quality for --mode fixed (default: %(default)s)")
    c.add_argument("--min-saving", type=float, default=15, help="minimum saving in %% (default: %(default)s)")
    c.add_argument("-m", "--match", action="append", help="only sheets whose URL contains this (repeatable)")
    c.add_argument("--limit", type=int, help="compress at most N sheets")
    c.add_argument("--include-unknown", action="store_true", help="also try sheets whose size is unknown")
    c.add_argument("--force", action="store_true", help="redo sheets already compressed with the same settings")
    c.add_argument("--no-auto-approve", action="store_true",
                   help="leave results pending for review in the GUI instead of exporting them to out/")
    c.add_argument("-w", "--workers", type=int, help="threads per sheet for scoring (default: up to 8)")

    m = command("map", "find the new URLs of uploaded files (and optionally rewrite the mod)", cmd_map)
    m.add_argument("--cloudinfo", action="append", help="TTS CloudInfo.bson to read (default: found automatically)")
    m.add_argument("--urls", help="text file with new URLs, in any order, instead of CloudInfo.bson")
    m.add_argument("--no-check", action="store_true", help="don't check the new URLs online")
    m.add_argument("--apply", action="store_true", help="rewrite old URLs to new ones in the scanned folders")
    m.add_argument("--dry-run", action="store_true", help="show what --apply would change, without writing")

    command("undo", "restore the files changed by the last apply", cmd_undo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
