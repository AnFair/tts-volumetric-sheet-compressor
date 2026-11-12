"""Project-level operations shared by the CLI and the GUI: scan, compress, review.

Everything works on the project state dicts and saves after each change.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from . import report, scanner, urls
from .compress import COMPRESSED, Result, Settings, compress_sheet
from .download import download_all, original_name
from .probe import NO_SIZE, probe_all
from .project import Project
from .units import format_size

# Approval values
AUTO = "auto"           # met the target, approved without review
APPROVED = "approved"   # approved by the user
REJECTED = "rejected"   # rejected by the user
PENDING = "pending"     # waiting for review

Sheet = dict[str, Any]
Log = Callable[[str], None]
Progress = Callable[[int, int], None]


def _quiet(*_: Any) -> None:
    pass


# --------------------------------------------------------------------------- scan


def scan_project(
    project: Project,
    paths: list[str],
    threshold: int,
    exclude: list[str] = (),
    refresh: bool = False,
    concurrency: int = 8,
    file_progress: Progress | None = None,
    url_progress: Progress | None = None,
    log: Log = _quiet,
    should_stop: Callable[[], bool] = lambda: False,
) -> tuple[dict[str, Any], scanner.ScanResult]:
    """Scan ``paths``, check sizes (reusing cached ones unless ``refresh``), save state and reports.

    Stopping keeps the sizes checked so far; the next scan checks only the rest.
    """
    result = scanner.scan(paths, exclude=exclude, skip=project.own_files, progress=file_progress)
    log(f"{result.files_total} JSON files, {result.files_with_hits} with decksheets, {len(result.sheets)} unique sheets")

    probes = {} if refresh else project.cached_probes()
    probes = {k: v for k, v in probes.items() if k in result.sheets}
    todo = [k for k in result.sheets if k not in probes]
    if todo and not should_stop():
        log(f"Checking size of {len(todo)} URLs ({len(probes)} cached)...")
        probes.update(probe_all(todo, concurrency=concurrency, progress=url_progress, should_stop=should_stop))
    if should_stop():
        unchecked = sum(1 for k in result.sheets if k not in probes)
        log(f"Stopped: {unchecked} URLs not checked yet. Scan again to check them.")

    state = project.save_scan([str(p) for p in paths], list(exclude), result, probes, threshold)
    report.write_reports(state, project.reports_dir)
    return state, result


# --------------------------------------------------------------------------- compress


def output_name(sheet: Sheet, ext: str) -> str:
    """``<deckkey>_<face|back|faceback>_<hash12>.<ext>`` - unique and easy to match after upload."""
    refs = sheet["refs"]
    deck = next((r["deck_key"] for r in refs if r.get("deck_key")), None) or "sheet"
    roles = {r["role"] for r in refs}
    role = "faceback" if len(roles) > 1 else next(iter(roles), "sheet")
    return f"{deck}_{role}_{urls.short_hash(sheet['key'], 12)}.{ext}"


def select(
    state: dict[str, Any],
    threshold: int,
    settings: Settings,
    include_unknown: bool = False,
    match: list[str] | None = None,
    force: bool = False,
) -> list[Sheet]:
    """Sheets over the threshold that haven't been compressed with the same settings yet."""

    def wanted(s: Sheet) -> bool:
        if match and not any(m.lower() in s["key"].lower() for m in match):
            return False
        over = report.over_threshold(s, threshold)
        if not (over or (over is None and include_unknown and report.probe_status(s) == NO_SIZE)):
            return False
        done = (s.get("compression") or {}).get("settings") == settings.signature()
        return force or not done

    return [s for s in state["sheets"] if wanted(s)]


def find(state: dict[str, Any], key: str) -> Sheet:
    return next(s for s in state["sheets"] if s["key"] == key)


def _record(project: Project, state: dict[str, Any], sheet: Sheet, res: Result, approval: str | None) -> None:
    """Replace the sheet's previous result with ``res`` and save. ``out/`` is only touched by export()."""
    if old := (sheet.get("compression") or {}).get("candidate_path"):
        Path(old).unlink(missing_ok=True)
    candidate_path = None
    if res.chosen:
        candidate_path = project.candidates_dir / f"{original_name(sheet['key'])}.{res.chosen.ext}"
        candidate_path.write_bytes(res.chosen.data)
    project.update_sheet(state, sheet["key"],
                         compression={**res.to_dict(), "candidate_path": str(candidate_path) if candidate_path else None},
                         approval=approval)


def download(
    project: Project,
    state: dict[str, Any],
    sheets: list[Sheet],
    progress: Progress | None = None,
    log: Log = _quiet,
    on_sheet_done: Callable[[str], None] = _quiet,
    should_stop: Callable[[], bool] = lambda: False,
) -> list[Sheet]:
    """Download the originals of ``sheets`` (files already there are reused). Returns the sheets that have one."""
    project.ensure()
    results = download_all([s["key"] for s in sheets], project.originals_dir, progress=progress,
                           should_stop=should_stop)
    ready = []
    for sheet in sheets:
        if (d := results.get(sheet["key"])) is None:
            continue                                   # stopped before it started
        sheet["download"] = {k: v for k, v in asdict(d).items() if k != "key"}
        if d.status == "ok":
            ready.append(sheet)
        else:
            log(f"{report.label(sheet)}: download {d.status}: {d.message}  {sheet['key']}")
        on_sheet_done(sheet["key"])
    project.save_state(state)
    log(f"Downloaded {len(ready)} of {len(sheets)} originals" + (" (stopped)" if should_stop() else ""))
    return ready


def run(
    project: Project,
    state: dict[str, Any],
    sheets: list[Sheet],
    settings: Settings,
    threshold: int,
    auto_approve: bool = True,
    log: Log = print,
    download_progress: Progress | None = None,
    progress: Progress | None = None,
    on_sheet_done: Callable[[str], None] = _quiet,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Download (if not done yet) and compress ``sheets``; each result is saved as soon as it's ready.

    Results that meet the target are flagged ``auto`` (or ``pending`` without auto-approve).
    """
    sheets = download(project, state, sheets, download_progress, log, on_sheet_done, should_stop)
    for n, sheet in enumerate(sheets, 1):
        if should_stop():
            log("Stopped.")
            break
        if progress:
            progress(n - 1, len(sheets))
        key, d = sheet["key"], sheet["download"]
        log(f"[{n}/{len(sheets)}] {report.label(sheet)}  {key}")
        if d["size"] <= threshold:
            log(f"    {format_size(d['size'])} is under the threshold, skipping")
        else:
            res = compress_sheet(d["path"], settings, log=lambda m: log(f"    {m}"))
            if res.status == COMPRESSED:
                approval = AUTO if auto_approve else PENDING
            else:
                approval = None
            _record(project, state, sheet, res, approval)
            log(f"    => {res.describe()}")
        on_sheet_done(key)
    if progress:
        progress(len(sheets), len(sheets))


def reencode(
    project: Project,
    state: dict[str, Any],
    key: str,
    settings: Settings,
    fmt: str,
    quality: int | None,
    log: Log = _quiet,
) -> Result:
    """Manual override: encode one sheet with a fixed format/quality; left pending for review."""
    sheet = find(state, key)
    path = (sheet.get("download") or {}).get("path")
    if not path or not Path(path).exists():
        raise FileNotFoundError("the original isn't downloaded yet; download the sheet first")
    res = compress_sheet(path, settings, log=log, fixed=(fmt, quality))
    _record(project, state, sheet, res, PENDING if res.chosen else None)
    return res


# --------------------------------------------------------------------------- review / export


def is_approved(sheet: Sheet) -> bool:
    return sheet.get("approval") in (AUTO, APPROVED)


def set_approval(project: Project, state: dict[str, Any], keys: list[str], approval: str) -> list[str]:
    """Flag sheets approved/rejected (no files are written until export). Returns keys it couldn't approve."""
    skipped = []
    for key in keys:
        sheet = find(state, key)
        if approval == APPROVED and not (sheet.get("compression") or {}).get("candidate_path"):
            skipped.append(key)
        else:
            sheet["approval"] = approval
    project.save_state(state)
    return skipped


def export(project: Project, state: dict[str, Any], progress: Progress | None = None,
           log: Log = _quiet) -> dict[str, int]:
    """Make ``out/`` match the approvals (copy approved results, remove the rest) and write the reports."""
    project.ensure()
    sheets = state["sheets"]
    copied = removed = 0
    for n, sheet in enumerate(sheets, 1):
        old = sheet.get("output")
        new = None
        candidate = (sheet.get("compression") or {}).get("candidate_path")
        if is_approved(sheet) and candidate and Path(candidate).exists():
            new = output_name(sheet, Path(candidate).suffix[1:])
            target = project.out_dir / new
            if not target.exists() or target.stat().st_size != Path(candidate).stat().st_size:
                shutil.copyfile(candidate, target)
                copied += 1
        if old and old != new:
            (project.out_dir / old).unlink(missing_ok=True)
            removed += 1
        sheet["output"] = new
        if progress and (n % 50 == 0 or n == len(sheets)):
            progress(n, len(sheets))
    project.save_state(state)
    report.write_reports(state, project.reports_dir)
    in_out = sum(1 for s in sheets if s.get("output"))
    log(f"Exported: {in_out} files in out/ ({copied} copied, {removed} removed); reports updated.")
    return {"in_out": in_out, "copied": copied, "removed": removed}
