"""Reports, all built from the project state.

CSV files are written with a BOM so Excel opens them as UTF-8.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from .compress import COMPRESSED, candidate_name
from .probe import OK, ProbeResult

Sheet = dict[str, Any]


# --------------------------------------------------------------------------- helpers


def label(sheet: Sheet) -> str:
    """Human name for a sheet: the first referencing object's nickname or name."""
    first = sheet["refs"][0] if sheet.get("refs") else {}
    return first.get("nickname") or first.get("name") or "?"


def size_of(sheet: Sheet) -> int | None:
    p = sheet.get("probe") or {}
    return p.get("size") if p.get("status") == OK else None


def probe_status(sheet: Sheet) -> str:
    return (sheet.get("probe") or {}).get("status") or "not-probed"


def probe_text(sheet: Sheet) -> str:
    """Human-readable scan result, e.g. 'blocked by host (HTTP 429 Too Many Requests)'."""
    p = sheet.get("probe")
    return ProbeResult(**p).describe() if p else "not checked"


def over_threshold(sheet: Sheet, threshold: int) -> bool | None:
    """None when the size is unknown."""
    size = size_of(sheet)
    return None if size is None else size > threshold


def result_in_use(sheet: Sheet) -> dict[str, Any]:
    """The compressed result *in use*: met the target, or approved by hand. Failed tries don't count."""
    comp = sheet.get("compression") or {}
    in_use = comp.get("status") == COMPRESSED or sheet.get("approval") == "approved"
    return (comp.get("chosen") or {}) if in_use else {}


def saving(sheet: Sheet) -> float | None:
    """Fraction saved by the result in use (0.89 = 89 % smaller)."""
    orig = ((sheet.get("compression") or {}).get("source") or {}).get("size")
    new = result_in_use(sheet).get("size")
    return 1 - new / orig if orig and new else None


def by_size(sheets: Iterable[Sheet]) -> list[Sheet]:
    return sorted(sheets, key=lambda s: (-(size_of(s) or -1), s["key"]))


def _yn(v: bool | None) -> str:
    return "" if v is None else ("yes" if v else "no")


def _write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(["" if v is None else v for v in row] for row in rows)


# --------------------------------------------------------------------------- scan


def scan_summary(state: dict[str, Any]) -> dict[str, Any]:
    sheets, threshold = state["sheets"], state["threshold"]
    by_status: dict[str, int] = {}
    for s in sheets:
        by_status[probe_status(s)] = by_status.get(probe_status(s), 0) + 1
    over = [s for s in sheets if over_threshold(s, threshold)]
    return {
        "files_total": state["files_total"],
        "files_with_hits": state["files_with_hits"],
        "threshold": threshold,
        "sheets": len(sheets),
        "references": sum(len(s["refs"]) for s in sheets),
        "by_status": by_status,
        "over_threshold": len(over),
        "over_threshold_bytes": sum(size_of(s) for s in over),
        "total_bytes": sum(size_of(s) or 0 for s in sheets),
    }


# --------------------------------------------------------------------------- compress


def compress_totals(sheets: Iterable[Sheet]) -> dict[str, Any]:
    """Status counts plus before/after bytes of the compressed sheets."""
    counts: dict[str, int] = {}
    before = after = 0
    for s in sheets:
        comp = s.get("compression") or {}
        status = comp.get("status") or f"download {(s.get('download') or {}).get('status', 'skipped')}"
        counts[status] = counts.get(status, 0) + 1
        if status == COMPRESSED:
            before += comp["source"]["size"]
            after += comp["chosen"]["size"]
    return {"counts": counts, "before": before, "after": after}


def final_status(sheet: Sheet, threshold: int) -> str:
    """Where a sheet stands end to end, e.g. 'replaced', 'under threshold', 'rejected'."""
    up = sheet.get("upload") or {}
    comp = sheet.get("compression") or {}
    approval = sheet.get("approval")
    if up.get("applied"):
        return "replaced"
    if up.get("url"):
        return "uploaded, not applied" if up.get("check") in (None, "ok") else f"new URL broken: {up['check']}"
    if sheet.get("output"):
        return "exported, not uploaded yet"
    if approval in ("auto", "approved"):
        return "approved, not exported yet"
    if approval in ("rejected", "pending"):
        return {"rejected": "rejected", "pending": "pending review"}[approval]
    if comp:
        return f"{comp['status']}: {comp.get('message') or ''}".rstrip(": ")
    if probe_status(sheet) != OK:
        return probe_text(sheet)                     # not checked, not found, blocked, ...
    return "over threshold, not compressed" if over_threshold(sheet, threshold) else "under threshold"


def write_reports(state: dict[str, Any], folder: Path) -> None:
    """final_report.csv: every sheet, where it stands, old and new URL and sizes.
    references.csv: every place a sheet's URL is used (file, JSON path, object)."""
    folder.mkdir(parents=True, exist_ok=True)
    sheets, threshold = by_size(state["sheets"]), state["threshold"]

    def sheet_row(s: Sheet) -> list[Any]:
        comp, up, res = s.get("compression") or {}, s.get("upload") or {}, result_in_use(s)
        src = comp.get("source") or {}
        return [
            final_status(s, threshold), label(s), len(s["refs"]), len({r["file"] for r in s["refs"]}),
            probe_text(s), src.get("size") or size_of(s), _yn(over_threshold(s, threshold)),
            src.get("format") or (s.get("download") or {}).get("format"),
            candidate_name(res["format"], res["quality"]) if res else None, res.get("score"), res.get("size"),
            None if saving(s) is None else f"{saving(s) * 100:.1f}",
            (up.get("applied") or {}).get("replacements"), s["key"], up.get("url"), s.get("output"),
        ]

    _write_csv(folder / "final_report.csv", [
        "status", "object", "used_by", "files", "scan", "old_size", "over_threshold", "old_format",
        "new_format", "score", "new_size", "saving_pct", "replacements", "old_url", "new_url", "uploaded_file",
    ], map(sheet_row, sheets))

    _write_csv(folder / "references.csv", [
        "url", "role", "object", "name", "guid", "deck_key", "num_width", "num_height", "unique_back",
        "file", "json_path",
    ], ([r["url"], r["role"], r["nickname"], r["name"], r["guid"], r["deck_key"], r["num_width"], r["num_height"],
         _yn(r["unique_back"]), r["file"], r["json_path"]] for s in sheets for r in s["refs"]))
