"""URL normalisation helpers.

The exact original string is always kept for rewriting; the normalised form is only used
for deduplication and for making HTTP requests.
"""

from __future__ import annotations

import hashlib
import re

# TTS sometimes prefixes URLs with tokens such as "{verifycache}".
_PREFIX_TOKENS = re.compile(r"^(\{[a-z_]+\})+", re.IGNORECASE)

# Legacy Steam cloud hosts that TTS rewrites to the Akamai host.
_STEAM_HOSTS = re.compile(
    r"^https?://(cloud-\d+\.steamusercontent\.com|steamusercontent-a\.akamaihd\.net)/",
    re.IGNORECASE,
)
_STEAM_CANONICAL = "https://steamusercontent-a.akamaihd.net/"

# .../ugc/<handle>/<SHA1>/
_STEAM_UGC = re.compile(r"/ugc/(\d+)/([0-9A-Fa-f]{40})/?", re.IGNORECASE)


# A URL inside any text (JSON, Lua, XML, BSON): stops at whitespace, quotes, brackets, backslash, NUL.
URL_TOKEN = re.compile(rb"https?://[^\s\"'<>\\\x00]+")


def steam_urls_by_sha1(data: bytes) -> dict[str, str]:
    """All Steam UGC URLs found in ``data``, keyed by the content SHA1 they embed (later ones win)."""
    found = {}
    for m in URL_TOKEN.finditer(data):
        url = m.group().decode("utf-8", "replace")
        if sha1 := steam_sha1(url):
            found[sha1] = normalize(url)
    return found


def normalize(url: str) -> str:
    """Canonical form used as the dedupe key and as the request URL."""
    u = url.strip()
    u = _PREFIX_TOKENS.sub("", u)
    u = _STEAM_HOSTS.sub(_STEAM_CANONICAL, u)
    return u


def steam_sha1(url: str) -> str | None:
    """The content SHA1 embedded in a Steam UGC URL (uppercase), if any."""
    m = _STEAM_UGC.search(url)
    return m.group(2).upper() if m else None


def short_hash(url: str, length: int = 8) -> str:
    """Short, stable id for a sheet: the Steam content hash if present, else a SHA1 of the URL."""
    m = _STEAM_UGC.search(url)
    if m:
        return m.group(2)[:length].upper()
    return hashlib.sha1(normalize(url).encode("utf-8")).hexdigest()[:length].upper()
