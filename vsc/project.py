"""Project folder and its persisted state (``state.json``), so runs are resumable.

The state is plain JSON-able dicts; everything after the scan works on it directly.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .probe import OK, ProbeResult
from .scanner import ScanResult

STATE_VERSION = 1


def _tool_dir() -> Path:
    """The folder the tool lives in (next to the .exe when packaged, else the source checkout)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


PROJECTS_DIR = _tool_dir() / "projects"


def project_path(name_or_path: str | os.PathLike) -> Path:
    """A bare name means ``projects/<name>`` in the tool folder; anything with a path in it is used as is."""
    p = Path(name_or_path)
    return p if p.is_absolute() or len(p.parts) > 1 else PROJECTS_DIR / p


class Project:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.reports_dir = self.root / "reports"
        self.originals_dir = self.root / "work" / "originals"
        self.candidates_dir = self.root / "work" / "candidates"
        self.out_dir = self.root / "out"

    @property
    def own_files(self) -> list[Path]:
        """The project's own files/folders - never scanned, even if the project sits inside an input."""
        return [self.state_path, self.state_path.with_suffix(".json.tmp"), self.reports_dir,
                self.root / "work", self.out_dir]

    def ensure(self) -> None:
        for d in (self.reports_dir, self.originals_dir, self.candidates_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def update_sheet(self, state: dict[str, Any], key: str, **fields: Any) -> None:
        """Merge ``fields`` into one sheet entry and persist immediately."""
        sheet = next(s for s in state["sheets"] if s["key"] == key)
        sheet.update(fields)
        self.save_state(state)

    def cached_probes(self) -> dict[str, ProbeResult]:
        """Successful probe results from a previous scan, keyed by sheet key."""
        return {
            s["key"]: ProbeResult(**s["probe"])
            for s in self.load_state().get("sheets", [])
            if (s.get("probe") or {}).get("status") == OK
        }

    def save_scan(self, inputs: list[str], exclude: list[str], result: ScanResult,
                  probes: dict[str, ProbeResult], threshold: int) -> dict[str, Any]:
        """Store a scan, keeping later-stage data (compression, mapping) of sheets seen before."""
        old = {s["key"]: s for s in self.load_state().get("sheets", [])}
        sheets = []
        for key, sheet in result.sheets.items():
            entry = old.get(key, {})
            entry.update(
                key=key,
                variants=sorted(sheet.variants),
                refs=[asdict(r) for r in sheet.refs],
                probe=asdict(probes[key]) if key in probes else None,
            )
            sheets.append(entry)
        state = {
            "version": STATE_VERSION,
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inputs": inputs,
            "exclude": exclude,
            "threshold": threshold,
            "files_total": result.files_total,
            "files_with_hits": result.files_with_hits,
            "file_errors": result.errors,
            "sheets": sheets,
            "history": self.load_state().get("history", []),   # past applies, for undo
        }
        self.save_state(state)
        return state
