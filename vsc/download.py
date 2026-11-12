"""Download original sheets into the project's work folder."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx

from . import net, urls

# Magic bytes -> extension
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),        # further checked below
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
]


def sniff(head: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            if ext == "webp" and head[8:12] != b"WEBP":
                continue
            return ext
    return None


@dataclass
class Download:
    key: str
    status: str                 # "ok" | "dead" | "not-image" | "error"
    path: str | None = None
    size: int | None = None
    sha1: str | None = None
    format: str | None = None
    message: str | None = None


def original_name(key: str) -> str:
    return urls.short_hash(key, 16)


def _existing(folder: Path, key: str) -> Download | None:
    for p in folder.glob(f"{original_name(key)}.*"):
        if p.suffix != ".part":
            data = p.read_bytes()
            return Download(key, "ok", str(p), len(data), hashlib.sha1(data).hexdigest().upper(), p.suffix[1:])
    return None


async def _download_once(client: httpx.AsyncClient, key: str, folder: Path) -> Download:
    stem = original_name(key)
    tmp = folder / f"{stem}.part"
    h = hashlib.sha1()
    size, head = 0, b""
    try:
        async with client.stream("GET", key) as r:
            net.raise_if_retryable(r)
            if r.status_code >= 400:
                return Download(key, "dead", message=net.http_status_text(r.status_code))
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    head = head or chunk[:16]
                    h.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
        ext = sniff(head)
        if ext is None:
            return Download(key, "not-image", size=size, message=f"unrecognised data {head[:8]!r}")
        sha1 = h.hexdigest().upper()
        expected = urls.steam_sha1(key)
        if expected and expected != sha1:
            # Steam URLs embed the file's SHA1; a mismatch means a truncated/corrupt download.
            raise httpx.TransportError(f"SHA1 mismatch (got {sha1[:8]}, expected {expected[:8]})")
        final = folder / f"{stem}.{ext}"
        os.replace(tmp, final)
        return Download(key, "ok", str(final), size, sha1, ext)
    finally:
        tmp.unlink(missing_ok=True)


def download_all(
    keys: Iterable[str],
    folder: Path,
    concurrency: int = 4,
    timeout: float = 120.0,
    retries: int = 2,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, Download]:
    """Download each key once; files already in ``folder`` are reused. Stopped keys are left out."""
    folder.mkdir(parents=True, exist_ok=True)
    results: dict[str, Download] = {}
    todo = []
    for k in dict.fromkeys(keys):
        if d := _existing(folder, k):
            results[k] = d
        else:
            todo.append(k)

    async def worker(client: httpx.AsyncClient, key: str) -> Download:
        try:
            return await net.retrying(lambda: _download_once(client, key, folder), retries)
        except (*net.RETRYABLE, OSError) as e:
            return Download(key, "error", message=net.describe(e))

    results.update(net.run_all(todo, worker, concurrency, timeout, progress, should_stop))
    return results
