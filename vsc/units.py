"""Byte sizes: parsing and display. KB/MB/GB are binary, as Windows Explorer shows them."""

from __future__ import annotations

import re

_SIZE = re.compile(r"^\s*([\d.]+)\s*([kmg]?)i?b?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(text: str) -> int:
    """'2MB' -> 2097152."""
    m = _SIZE.match(text)
    if not m:
        raise ValueError(f"invalid size: {text!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def format_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"
