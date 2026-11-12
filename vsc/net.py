"""Shared HTTP plumbing: one client setup, one retry policy, one bounded-concurrency runner."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Awaitable, Callable, Hashable, Iterable, TypeVar

import httpx

from . import __version__

USER_AGENT = f"VolumetricSheetCompressor/{__version__}"

# Errors worth retrying. Call raise_if_retryable() on a response to retry server errors and 429s.
RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)

K = TypeVar("K", bound=Hashable)
R = TypeVar("R")


def raise_if_retryable(r: httpx.Response) -> None:
    if r.status_code >= 500 or r.status_code == 429:
        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)


def http_status_text(code: int) -> str:
    """'HTTP 404 Not Found'."""
    try:
        return f"HTTP {code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"HTTP {code}"


def describe(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return http_status_text(e.response.status_code)
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def retrying(attempt: Callable[[], Awaitable[R]], retries: int) -> R:
    """Run ``attempt`` up to ``retries + 1`` times with exponential backoff on RETRYABLE errors."""
    for n in range(retries + 1):
        try:
            return await attempt()
        except RETRYABLE:
            if n == retries:
                raise
            await asyncio.sleep(1.5 * 2**n)
    raise AssertionError("unreachable")


def run_all(
    items: Iterable[K],
    worker: Callable[[httpx.AsyncClient, K], Awaitable[R]],
    concurrency: int,
    timeout: float,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[K, R]:
    """Run ``worker(client, item)`` for each unique item, at most ``concurrency`` at a time.

    Once ``should_stop()`` is true no new item starts; items already running finish, and only
    finished items are in the result.
    """
    todo = list(dict.fromkeys(items))
    results: dict[K, R] = {}

    async def main() -> None:
        sem = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=20.0),
            limits=httpx.Limits(max_connections=concurrency),
            headers={"User-Agent": USER_AGENT},
        ) as client:

            async def one(item: K) -> None:
                async with sem:
                    if should_stop():
                        return
                    results[item] = await worker(client, item)
                    if progress:
                        progress(len(results), len(todo))

            await asyncio.gather(*(one(i) for i in todo))

    if todo:
        asyncio.run(main())
    return results
