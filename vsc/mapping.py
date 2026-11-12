"""After upload: find each exported file's new Steam URL (``map``), rewrite the mod's text files
(``apply``) with backups, and ``undo`` the last apply.

Matching is by content: a Steam UGC URL embeds the SHA1 of the uploaded file, so the URL whose SHA1
equals an exported file's SHA1 is that file's upload - whatever its name or Cloud Manager folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import report, urls
from .pipeline import is_approved
from .probe import OK, probe_all
from .project import Project
from .scanner import TEXT_SUFFIXES, iter_files

TTS_APP_ID = "286160"
Progress = Callable[[int, int], None]
Log = Callable[[str], None]


def _quiet(*_: Any) -> None:
    pass


# --------------------------------------------------------------------------- finding uploads


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
        roots.append(Path(r"C:\Program Files (x86)\Steam"))
    else:
        home = Path.home()
        roots += [home / ".local/share/Steam", home / ".steam/steam",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
                  home / "Library/Application Support/Steam"]
    return list(dict.fromkeys(r.resolve() for r in roots if r.exists()))


def steam_userdata() -> Path | None:
    """The Steam ``userdata`` folder (one subfolder per Steam account), if Steam is installed."""
    return next((r / "userdata" for r in _steam_roots() if (r / "userdata").is_dir()), None)


def find_cloudinfo() -> list[Path]:
    """TTS's Cloud Manager index for every Steam account on this machine, most recently changed first."""
    found = []
    for root in _steam_roots():
        found += (root / "userdata").glob(f"*/{TTS_APP_ID}/remote/CloudInfo.bson")
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def cloudinfo_hint() -> str:
    """Where TTS keeps its upload list on this system, for people looking for it by hand."""
    steam = {"win32": r"C:\Program Files (x86)\Steam", "darwin": "~/Library/Application Support/Steam"}.get(
        sys.platform, "~/.local/share/Steam")
    sep = "\\" if sys.platform == "win32" else "/"
    return sep.join([str(steam_userdata() or f"{steam}{sep}userdata"), "<account number>", TTS_APP_ID, "remote",
                     "CloudInfo.bson"])


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def map_uploads(
    project: Project,
    state: dict[str, Any],
    sources: list[bytes],
    check: bool = True,
    log: Log = _quiet,
    progress: Progress | None = None,
) -> dict[str, int]:
    """Give every exported sheet its new URL, found by SHA1 in ``sources`` (CloudInfo.bson or pasted text)."""
    found: dict[str, str] = {}
    for data in sources:
        found.update(urls.steam_urls_by_sha1(data))
    log(f"{len(found)} Steam URLs found in the upload list")

    exported = [s for s in state["sheets"] if s.get("output") and (project.out_dir / s["output"]).exists()]
    for s in exported:
        sha1 = sha1_of(project.out_dir / s["output"])
        new_url = found.get(sha1)
        old = s.get("upload") or {}
        s["upload"] = {"sha1": sha1, "url": new_url, "size": (project.out_dir / s["output"]).stat().st_size,
                       "check": None, "applied": old.get("applied") if old.get("url") == new_url else None}

    mapped = [s for s in exported if s["upload"]["url"]]
    if check and mapped:
        log(f"Checking {len(mapped)} new URLs online...")
        probes = probe_all([s["upload"]["url"] for s in mapped], progress=progress)
        for s in mapped:
            p = probes[s["upload"]["url"]]
            ok = p.status == OK and p.size == s["upload"]["size"]
            s["upload"]["check"] = "ok" if ok else (
                f"size mismatch ({p.size} vs {s['upload']['size']})" if p.status == OK else p.describe())
    project.save_state(state)
    report.write_reports(state, project.reports_dir)

    counts = {
        "exported": len(exported),
        "mapped": len(mapped),
        "broken": sum(1 for s in mapped if s["upload"]["check"] not in (None, "ok")),
        "not_uploaded": len(exported) - len(mapped),
    }
    log(f"Mapped {counts['mapped']} of {counts['exported']} exported sheets; "
        f"{counts['broken']} new URLs failed the check; {counts['not_uploaded']} not uploaded yet.")
    return counts


# --------------------------------------------------------------------------- apply / undo


def ready_to_apply(sheet: dict[str, Any]) -> bool:
    up = sheet.get("upload") or {}
    return bool(is_approved(sheet) and up.get("url") and up.get("check") in (None, "ok"))


def _replacements(state: dict[str, Any]) -> dict[str, str]:
    """Normalised old URL -> new URL for every sheet ready to apply."""
    return {s["key"]: s["upload"]["url"] for s in state["sheets"] if ready_to_apply(s)}


def _is_json(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8-sig"))
        return True
    except ValueError:
        return False


def _rewrite(data: bytes, table: dict[str, str], counts: dict[str, int]) -> bytes:
    """Replace every URL in ``data`` whose normalised form is in ``table``; bytes elsewhere are untouched."""

    def repl(m):
        key = urls.normalize(m.group().decode("utf-8", "replace"))
        new = table.get(key)
        if new is None:
            return m.group()
        counts[key] = counts.get(key, 0) + 1
        return new.encode("utf-8")

    return urls.URL_TOKEN.sub(repl, data)


def apply(
    project: Project,
    state: dict[str, Any],
    dry_run: bool = False,
    log: Log = _quiet,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Rewrite old URLs to new ones in every text file under the scanned inputs (backups first)."""
    table = _replacements(state)
    if not table:
        log("Nothing to apply: no approved sheet has a working new URL.")
        return {"files": [], "replacements": 0, "sheets": {}, "errors": [], "backup": None}

    files = list(iter_files(state["inputs"], state.get("exclude", []), project.own_files, TEXT_SUFFIXES))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = project.root / "backup" / stamp
    per_sheet: dict[str, int] = {}
    changed: list[dict[str, Any]] = []
    errors: list[str] = []

    for n, path in enumerate(files, 1):
        if progress and (n % 500 == 0 or n == len(files)):
            progress(n, len(files))
        data = path.read_bytes()
        if b"http" not in data:
            continue
        counts: dict[str, int] = {}
        new = _rewrite(data, table, counts)
        if new == data:
            continue
        entry = {"path": str(path), "replacements": sum(counts.values())}
        if not dry_run:
            try:
                if path.suffix.lower() == ".json" and _is_json(data) and not _is_json(new):
                    raise ValueError("the result would not be valid JSON")   # never break a valid file
                backup = backup_dir / "files" / f"{len(changed):06d}{path.suffix}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
                tmp = path.with_name(path.name + ".vsc-tmp")
                tmp.write_bytes(new)
                os.replace(tmp, path)
                entry["backup"] = str(backup)
            except (ValueError, OSError) as e:
                errors.append(f"{path}: {e}")
                continue
        for k, v in counts.items():
            per_sheet[k] = per_sheet.get(k, 0) + v
        changed.append(entry)

    total = sum(e["replacements"] for e in changed)
    verb = "Would replace" if dry_run else "Replaced"
    log(f"{verb} {total} URLs in {len(changed)} of {len(files)} files ({len(per_sheet)} sheets)"
        + (f"; {len(errors)} files failed" if errors else ""))

    if not dry_run and changed:
        when = datetime.now().isoformat(timespec="seconds")
        for s in state["sheets"]:
            if s["key"] in per_sheet:
                s["upload"]["applied"] = {"at": when, "replacements": per_sheet[s["key"]]}
        state.setdefault("history", []).append({"at": when, "backup": str(backup_dir), "files": changed})
        (backup_dir / "manifest.json").write_text(json.dumps(changed, indent=1, ensure_ascii=False), encoding="utf-8")
        project.save_state(state)
        report.write_reports(state, project.reports_dir)
        log(f"Backups: {backup_dir}")
    return {"files": changed, "replacements": total, "sheets": per_sheet, "errors": errors,
            "backup": None if dry_run else str(backup_dir)}


def undo(project: Project, state: dict[str, Any], log: Log = _quiet) -> int:
    """Restore the files changed by the last apply. Returns how many were restored."""
    history = state.get("history") or []
    if not history:
        log("Nothing to undo.")
        return 0
    last = history.pop()
    for entry in last["files"]:
        shutil.copy2(entry["backup"], entry["path"])
    for s in state["sheets"]:
        if ((s.get("upload") or {}).get("applied") or {}).get("at") == last["at"]:
            s["upload"]["applied"] = None
    project.save_state(state)
    report.write_reports(state, project.reports_dir)
    log(f"Restored {len(last['files'])} files from {last['backup']}")
    return len(last["files"])
