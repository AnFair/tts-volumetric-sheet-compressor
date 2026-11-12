"""Find every dict with both ``FaceURL`` and ``BackURL`` in a set of JSON files."""

from __future__ import annotations

import fnmatch
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import urls

_MARKER = b"FaceURL"


@dataclass
class Reference:
    """One occurrence of a sheet URL inside a JSON file."""

    file: str
    json_path: str          # e.g. "ObjectStates[0].ContainedObjects[3].CustomDeck.2665"
    role: str               # "face" or "back"
    url: str                # exact original string
    deck_key: str | None
    num_width: int | None
    num_height: int | None
    unique_back: bool
    guid: str | None        # enclosing TTS object
    name: str | None
    nickname: str | None


@dataclass
class Sheet:
    """One unique image (by normalised URL) and everything that references it."""

    key: str                                   # normalised URL
    variants: set[str] = field(default_factory=set)   # exact spellings, for rewriting
    refs: list[Reference] = field(default_factory=list)


@dataclass
class ScanResult:
    sheets: dict[str, Sheet] = field(default_factory=dict)
    files_total: int = 0
    files_with_hits: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)   # (file, message)


# --------------------------------------------------------------------------- files


# Text files a TTS mod keeps URLs in (JSON objects, Lua scripts and their saved state, notes, UI XML).
TEXT_SUFFIXES = (".json", ".ttslua", ".lua", ".luascriptstate", ".gmnotes", ".xml", ".txt")


def iter_files(
    paths: Iterable[str | os.PathLike], exclude: Iterable[str] = (), skip: Iterable[Path] = (),
    suffixes: tuple[str, ...] = (".json",),
) -> Iterator[Path]:
    """Yield resolved files with one of ``suffixes`` under ``paths``.

    ``exclude`` globs match against the posix path or the name; ``skip`` are exact files/folders.
    """
    patterns = list(exclude)
    skipped = {Path(p).resolve() for p in skip}

    def excluded(p: Path) -> bool:
        s = p.as_posix()
        return p in skipped or any(fnmatch.fnmatch(s, pat) or fnmatch.fnmatch(p.name, pat) for pat in patterns)

    for root in paths:
        root = Path(root).resolve()
        if root.is_file():
            if not excluded(root):
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            dirnames[:] = [n for n in dirnames if not n.startswith(".") and not excluded(d / n)]
            for n in filenames:
                if n.lower().endswith(suffixes):
                    p = d / n
                    if not excluded(p):
                        yield p


# --------------------------------------------------------------------------- parsing


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _walk(node: Any, path: str, owner: dict | None, file: str, out: list[Reference]) -> None:
    if isinstance(node, dict):
        if "GUID" in node or "Name" in node:
            owner = node
        face, back = node.get("FaceURL"), node.get("BackURL")
        if isinstance(face, str) and isinstance(back, str):
            parent, _, last = path.rpartition(".")
            deck_key = last if parent.rpartition(".")[2] == "CustomDeck" else None
            common = dict(
                file=file,
                json_path=path,
                deck_key=deck_key,
                num_width=_as_int(node.get("NumWidth")),
                num_height=_as_int(node.get("NumHeight")),
                unique_back=bool(node.get("UniqueBack", False)),
                guid=owner.get("GUID") if owner else None,
                name=owner.get("Name") if owner else None,
                nickname=owner.get("Nickname") if owner else None,
            )
            for role, url in (("face", face), ("back", back)):
                if url.strip():
                    out.append(Reference(role=role, url=url, **common))
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                _walk(v, f"{path}.{k}" if path else str(k), owner, file, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, (dict, list)):
                _walk(v, f"{path}[{i}]", owner, file, out)


def scan_file(path: Path) -> tuple[list[Reference], str | None]:
    """Return references found in one file and an error message (or None)."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return [], f"read error: {e}"
    if _MARKER not in data:
        return [], None
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return [], f"invalid JSON: {e}"
    refs: list[Reference] = []
    _walk(doc, "", None, str(path), refs)
    return refs, None


def scan(
    paths: Iterable[str | os.PathLike],
    exclude: Iterable[str] = (),
    skip: Iterable[Path] = (),
    workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
) -> ScanResult:
    """Scan files and group references into unique sheets."""
    result = ScanResult()
    # Dedupe resolved paths, so overlapping inputs (a folder and a file inside it) count once.
    files = list(dict.fromkeys(iter_files(paths, exclude, skip)))
    result.files_total = len(files)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (refs, err) in enumerate(pool.map(scan_file, files), 1):
            if err:
                result.errors.append((str(files[i - 1]), err))
            if refs:
                result.files_with_hits += 1
                for r in refs:
                    key = urls.normalize(r.url)
                    sheet = result.sheets.get(key)
                    if sheet is None:
                        sheet = result.sheets[key] = Sheet(key=key)
                    sheet.variants.add(r.url)
                    sheet.refs.append(r)
            if progress and (i % 500 == 0 or i == len(files)):
                progress(i, len(files))
    return result
