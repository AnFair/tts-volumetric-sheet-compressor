"""Remote size / type checks for sheet URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx

from . import net

# Statuses
OK = "ok"                # size known
NO_SIZE = "no-size"      # reachable, but the server didn't tell us the size
DEAD = "dead"            # HTTP 4xx: the file is gone
BLOCKED = "blocked"      # HTTP 429 after retries: the host refuses us (rate limit or block)
NOT_IMAGE = "not-image"  # e.g. a Dropbox / Google Drive HTML page
ERROR = "error"          # network / timeout / server error

_CONTENT_RANGE = re.compile(r"bytes\s+\d+-\d+/(\d+)", re.IGNORECASE)


@dataclass
class ProbeResult:
    url: str
    status: str
    size: int | None = None
    content_type: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    message: str | None = None

    def describe(self) -> str:
        """Human-readable status, e.g. 'not found (HTTP 404 Not Found)'."""
        http = net.http_status_text(self.http_status) if self.http_status else ""
        return {
            OK: "ok",
            NO_SIZE: "size unknown",
            DEAD: f"not found ({http})",
            BLOCKED: f"blocked by host ({http})",
            NOT_IMAGE: f"not an image ({self.content_type or 'unknown type'})",
        }.get(self.status, f"error ({self.message})")


def _is_html(ct: str | None) -> bool:
    return bool(ct) and ("text/html" in ct or "application/xhtml" in ct)


def _int(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() and int(v) > 0 else None


async def _probe_once(client: httpx.AsyncClient, url: str) -> ProbeResult:
    def result(status: str, r: httpx.Response, size: int | None = None) -> ProbeResult:
        return ProbeResult(url, status, size, r.headers.get("content-type"), r.status_code, str(r.url))

    r = await client.head(url)
    if r.status_code < 400:
        if _is_html(r.headers.get("content-type")):
            return result(NOT_IMAGE, r)
        if size := _int(r.headers.get("content-length")):
            return result(OK, r, size)

    # HEAD failed or gave no length: ask for one byte. Never trust a HEAD 404 -
    # Steam's CDN returns 404 to HEAD for files that GET serves fine.
    async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as g:
        net.raise_if_retryable(g)
        if g.status_code >= 400:
            return result(DEAD, g)
        if _is_html(g.headers.get("content-type")):
            return result(NOT_IMAGE, g)
        if m := _CONTENT_RANGE.search(g.headers.get("content-range", "")):
            return result(OK, g, int(m.group(1)))
        if g.status_code == 200 and (size := _int(g.headers.get("content-length"))):
            return result(OK, g, size)
        return result(NO_SIZE, g)


def probe_all(
    url_list: Iterable[str],
    concurrency: int = 8,
    timeout: float = 20.0,
    retries: int = 2,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, ProbeResult]:
    async def worker(client: httpx.AsyncClient, url: str) -> ProbeResult:
        try:
            return await net.retrying(lambda: _probe_once(client, url), retries)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            return ProbeResult(url, BLOCKED if code == 429 else ERROR, http_status=code, message=net.describe(e))
        except net.RETRYABLE as e:
            return ProbeResult(url, ERROR, message=net.describe(e))

    return net.run_all(url_list, worker, concurrency, timeout, progress, should_stop)
