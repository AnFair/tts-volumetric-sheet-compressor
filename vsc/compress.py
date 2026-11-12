"""Candidate encoding and quality search for one sheet.

For each lossy format the lowest quality whose *worst tile* still meets the target is
found. The search runs on a small set of the hardest tiles and is verified on all
tiles at the end; any tile that fails verification joins the search set.
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageFile

from .metrics import TileScorer
from .units import format_size

# Worst-tile SSIMULACRA2 targets. Meaning of the scale (libjxl): 90 = indistinguishable when
# flipping at 1:1, 85 = when flipping at normal viewing distance, 80 = side by side, 70 = high quality.
PRESETS = {
    "strict": 90.0,
    "excellent": 85.0,
    "very-high": 80.0,
    "high": 70.0,
}

# Status values
COMPRESSED = "compressed"   # a candidate met the target and saves enough
SKIPPED = "skipped"         # met the target, but saving < min_saving
FAILED = "failed"           # no candidate met the target (the best one is still kept for review)
ERROR = "error"

# Modes
FIRST = "first"         # formats in priority order; keep the first that meets the target and saves enough
SMALLEST = "smallest"   # try every format; keep the smallest that meets the target
FIXED = "fixed"         # one format/quality for everything (still scored)
MODES = (FIRST, SMALLEST, FIXED)

FORMATS = ("jpeg", "webp", "png")      # default priority order
FIXED_FORMATS = ("jpeg", "webp", "jpeg-keep")   # what a fixed / manual encode can use
_EXT = {"webp": "webp", "jpeg": "jpg", "jpeg-keep": "jpg", "png": "png"}


def candidate_name(fmt: str, quality: int | None) -> str:
    """'jpeg q94', 'jpeg-keep', 'png'."""
    return f"{fmt} q{quality}" if quality else fmt


@dataclass
class Settings:
    target: float = PRESETS["very-high"]
    mode: str = FIRST
    formats: tuple[str, ...] = FORMATS  # allowed formats, in priority order
    fixed_format: str = "jpeg"          # FIXED mode only
    fixed_quality: int | None = 85      # FIXED mode only (None for jpeg-keep)
    min_saving: float = 0.15
    workers: int = max(1, min(8, os.cpu_count() or 1))
    subset: int = 6            # hardest tiles used during the search
    q_min: int = 50
    q_max: int = 100
    webp_method: int = 4

    def describe(self) -> str:
        """e.g. 'target 80, first that passes of jpeg > webp > png, min saving 15%'."""
        how = {FIRST: "first that passes of", SMALLEST: "smallest of"}.get(self.mode)
        how = f"{how} {' > '.join(self.formats)}" if how else \
            f"fixed {candidate_name(self.fixed_format, self.fixed_quality)}"
        return f"target {self.target:g}, {how}, min saving {self.min_saving:.0%}"

    def signature(self) -> dict:
        """The settings that affect the result; a sheet is redone when these change."""
        sig = {"target": self.target, "mode": self.mode, "min_saving": self.min_saving, "formats": list(self.formats)}
        if self.mode == FIXED:
            sig["fixed"] = [self.fixed_format, self.fixed_quality]
        return sig


@dataclass
class Candidate:
    format: str                 # "webp" | "jpeg" | "jpeg-keep" | "png"
    quality: int | None
    size: int
    score: float | None         # worst-tile score (None = lossless, not measured)
    worst_tile: int | None = None
    passed: bool = False
    data: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def ext(self) -> str:
        return _EXT[self.format]

    @property
    def name(self) -> str:
        return candidate_name(self.format, self.quality)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("data")
        return d


@dataclass
class SourceInfo:
    format: str
    width: int
    height: int
    mode: str
    alpha_used: bool
    size: int


@dataclass
class Result:
    status: str
    source: SourceInfo | None
    chosen: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    message: str | None = None
    seconds: float = 0.0
    settings: dict = field(default_factory=dict)

    def describe(self) -> str:
        """One line for logs, e.g. 'compressed: jpeg q94 3.82 MB (-56%), worst tile 90.41 [19s]'."""
        text = self.status
        if c := self.chosen:
            score = "lossless" if c.score is None else c.score
            text += f": {c.name} {format_size(c.size)} (-{1 - c.size / self.source.size:.0%}), worst tile {score}"
        if self.message:
            text += f" - {self.message}"
        return f"{text}  [{self.seconds:.0f}s]"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "source": asdict(self.source) if self.source else None,
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "message": self.message,
            "seconds": round(self.seconds, 1),
            "settings": self.settings,
        }


# --------------------------------------------------------------------------- images


def load(path: str | os.PathLike) -> tuple[Image.Image, Image.Image, np.ndarray, SourceInfo]:
    """Return (raw image, working RGB/RGBA image, uint8 array, info). Unused alpha is dropped."""
    raw = Image.open(path)
    raw.load()
    has_alpha = raw.mode in ("RGBA", "LA", "PA") or (raw.mode == "P" and "transparency" in raw.info)
    work = raw.convert("RGBA" if has_alpha else "RGB")
    arr = np.asarray(work)
    if has_alpha and int(arr[..., 3].min()) == 255:
        work = work.convert("RGB")
        arr = np.asarray(work)
        has_alpha = False
    info = SourceInfo(
        format=(raw.format or "").lower(),
        width=raw.width,
        height=raw.height,
        mode=raw.mode,
        alpha_used=has_alpha,
        size=os.path.getsize(path),
    )
    return raw, work, arr, info


def encode(img: Image.Image, fmt: str, quality: int | None, settings: Settings, raw: Image.Image | None = None) -> bytes:
    buf = io.BytesIO()
    icc = img.info.get("icc_profile")
    extra = {"icc_profile": icc} if icc else {}
    if fmt in ("jpeg", "jpeg-keep"):
        # Optimised/progressive JPEG needs the whole output in one buffer, and Pillow guesses
        # ~1 byte/pixel below q95 - too small for noisy 4:4:4 art ("Suspension not allowed here").
        saved = ImageFile.MAXBLOCK
        ImageFile.MAXBLOCK = max(saved, 4 * img.width * img.height + (1 << 20))
        try:
            if fmt == "jpeg":
                img.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True, progressive=True, **extra)
            else:
                # Re-encode with the source's own quantisation tables and subsampling: minimal generation loss.
                assert raw is not None
                raw.save(buf, "JPEG", quality="keep", optimize=True, progressive=True, **extra)
        finally:
            ImageFile.MAXBLOCK = saved
    elif fmt == "webp":
        img.save(buf, "WEBP", quality=quality, method=settings.webp_method, alpha_quality=100, **extra)
    else:
        raise ValueError(fmt)
    return buf.getvalue()


def decode(data: bytes, mode: str) -> np.ndarray:
    im = Image.open(io.BytesIO(data))
    im.load()
    return np.asarray(im.convert(mode))


# --------------------------------------------------------------------------- search


class _Search:
    def __init__(self, work: Image.Image, raw: Image.Image, scorer: TileScorer, settings: Settings,
                 log: Callable[[str], None]):
        self.work, self.raw, self.scorer, self.s, self.log = work, raw, scorer, settings, log

    def _measure(self, fmt: str, q: int | None, indices=None, data: bytes | None = None
                 ) -> tuple[bytes, dict[int, float]]:
        """Encode (unless ``data`` is given), decode and score.
        With ``indices`` only those tiles, keeping their original side cached."""
        data = data if data is not None else encode(self.work, fmt, q, self.s, self.raw)
        dec = decode(data, self.work.mode)
        scores = self.scorer.scores(dec, indices, cache=indices is not None, workers=self.s.workers)
        return data, scores

    def full(self, fmt: str, q: int | None, data: bytes | None = None) -> tuple[Candidate, dict[int, float]]:
        """Candidate scored on every tile, plus the per-tile scores."""
        data, scores = self._measure(fmt, q, data=data)
        worst = min(scores, key=scores.get)
        c = Candidate(fmt, q, len(data), round(scores[worst], 2), worst,
                      passed=scores[worst] >= self.s.target, data=data)
        return c, scores

    def lossy(self, fmt: str, max_size: int) -> tuple[Candidate | None, bool, str]:
        """Lowest-quality passing candidate. Otherwise (None, too_big, why-not).

        Size grows with quality, so once a *failing* quality is already over ``max_size``,
        every passing quality is too: the search stops there (too_big).
        """
        s = self.s
        target = s.target
        probe_q = 90
        first, scores = self.full(fmt, probe_q)
        self.log(f"{fmt} q{probe_q}: worst {first.score:.2f}, {format_size(first.size)}")
        subset = sorted(scores, key=scores.get)[: s.subset]
        too_big_note = f"reaching score {target:g} needs more than {format_size(max_size)}"

        # best = lowest-quality candidate verified on all tiles so far.
        if first.passed:
            best, lo, hi = first, s.q_min, probe_q - 1
        else:
            if first.size > max_size:
                return None, True, too_big_note
            # Cheap reachability check before searching upwards.
            _, sub = self._measure(fmt, s.q_max, subset)
            if min(sub.values()) < target:
                return None, False, f"best score {min(sub.values()):.1f} even at q{s.q_max}"
            best, lo, hi = None, probe_q + 1, s.q_max

        while lo <= hi:
            # Binary search on the subset for the lowest q in [lo, hi] that meets the target.
            a, b, found = lo, hi, None
            while a <= b:
                mid = (a + b) // 2
                data, sub = self._measure(fmt, mid, subset)
                if min(sub.values()) >= target:
                    found, b = mid, mid - 1
                elif len(data) > max_size:
                    return (best, False, "") if best else (None, True, too_big_note)
                else:
                    a = mid + 1
            if found is None:
                break
            cand, cs = self.full(fmt, found)
            self.log(f"{fmt} q{found}: worst {cand.score:.2f}, {format_size(cand.size)} (verify)")
            if cand.passed:
                return cand, False, ""
            # Some other tile failed: add the failing tiles to the subset and search above.
            fails = sorted((i for i, v in cs.items() if v < target), key=cs.get)
            subset = list(dict.fromkeys(subset + fails[: s.subset]))
            lo = found + 1
        return (best, False, "") if best else (None, False, f"no quality up to q{s.q_max} reached score {target:g}")


def _search_all(path, raw, work, info: SourceInfo, search: _Search, settings: Settings,
                res: Result, log: Callable[[str], None]) -> tuple[bool, list[str]]:
    """Try the allowed formats in priority order (FIRST mode stops at the first usable result).

    Returns (some format was cut short only by min_saving, why-nots).
    """
    # Anything bigger than this can't be chosen, so don't spend time measuring it.
    max_size = int(info.size * (1 - settings.min_saving))
    too_big, notes = False, []

    def note(text: str) -> None:
        notes.append(text)
        log(text)

    def jpeg_keep() -> Candidate | None:
        nonlocal too_big
        data = encode(work, "jpeg-keep", None, settings, raw)
        if len(data) > max_size:
            note(f"jpeg-keep: {format_size(len(data))}, saves less than {settings.min_saving:.0%}")
            too_big = True
            return None
        c, _ = search.full("jpeg-keep", None, data)
        log(f"jpeg-keep: worst {c.score:.2f}, {format_size(c.size)}")
        return c

    def lossy(fmt: str) -> Candidate | None:
        nonlocal too_big
        c, cut_short, why = search.lossy(fmt, max_size)
        if c is None:
            note(f"{fmt}: {why}")
            too_big |= cut_short
        return c

    def png_lossless() -> Candidate:
        import oxipng
        t = time.monotonic()
        data = oxipng.optimize_from_memory(Path(path).read_bytes(), level=2)
        log(f"png lossless: {format_size(len(data))}  [{time.monotonic() - t:.0f}s]")
        return Candidate("png", None, len(data), None, passed=True, data=data)

    # What each format means for this source. Lossless PNG only helps a PNG source;
    # "jpeg-keep" (the source's own JPEG settings) only exists for a JPEG source.
    attempts: dict[str, list[Callable[[], Candidate | None]]] = {
        "jpeg": ([jpeg_keep] if info.format == "jpeg" else []) + [lambda: lossy("jpeg")],
        "webp": [lambda: lossy("webp")],
        "png": [png_lossless] if info.format == "png" else [],
    }
    for fmt in settings.formats:
        if fmt == "jpeg" and info.alpha_used:
            note("jpeg: skipped, the sheet uses transparency")
            continue
        for attempt in attempts[fmt]:
            c = attempt()
            if c is None:
                continue
            res.candidates.append(c)
            if settings.mode == FIRST and c.passed and c.size <= max_size:
                log(f"{c.name} is good enough: stopping (first-passing mode)")
                return too_big, notes
    return too_big, notes


def _classify(res: Result, settings: Settings, too_big: bool, notes: list[str]) -> None:
    """Pick the smallest passing candidate (else the best-scoring one, for review) and set status + message.

    In FIRST mode the search already stopped at the first usable candidate, so this picks that one.
    """
    passing = [c for c in res.candidates if c.passed]
    if passing:
        res.chosen = min(passing, key=lambda c: c.size)
    elif res.candidates:
        res.chosen = max(res.candidates, key=lambda c: c.score or 0)
    saving = 1 - res.chosen.size / res.source.size if res.chosen else 0.0
    why = f" ({'; '.join(notes)})" if notes else ""
    if passing and saving >= settings.min_saving:
        res.status = COMPRESSED
    elif passing:
        res.status = SKIPPED
        res.message = f"best result saves only {saving:.0%} (minimum {settings.min_saving:.0%})"
    elif too_big:
        res.status = SKIPPED
        res.message = f"no format saves {settings.min_saving:.0%} at score {settings.target:g}{why}"
    elif res.chosen:
        res.status = FAILED
        res.message = f"score {res.chosen.score} is below the target {settings.target:g}"
    else:
        res.status = FAILED
        res.message = f"no format reached score {settings.target:g}{why}"


def compress_sheet(
    path: str | os.PathLike,
    settings: Settings,
    log: Callable[[str], None] | None = None,
    fixed: tuple[str, int | None] | None = None,
) -> Result:
    """Compress one sheet according to ``settings.mode``. ``fixed=(format, quality)`` overrides the mode
    with one encode (a manual re-encode), scored the same way."""
    log = log or (lambda m: None)
    if fixed is None and settings.mode == FIXED:
        fixed = (settings.fixed_format, settings.fixed_quality)
    t0 = time.monotonic()
    try:
        raw, work, arr, info = load(path)
    except Exception as e:  # noqa: BLE001 - report any decode failure
        return Result(ERROR, None, message=f"cannot read image: {e}")

    res = Result(ERROR, info, settings=settings.signature())
    try:
        scorer = TileScorer(arr)
        search = _Search(work, raw, scorer, settings, log)
        log(f"{info.width}x{info.height} {info.format} {format_size(info.size)}, "
            f"{len(scorer.tiles)} tiles{', alpha' if info.alpha_used else ''}")
        if fixed:
            c, _ = search.full(*fixed)
            log(f"{c.name}: worst {c.score:.2f}, {format_size(c.size)}")
            res.candidates.append(c)
            too_big, notes = False, []
        else:
            too_big, notes = _search_all(path, raw, work, info, search, settings, res, log)
        _classify(res, settings, too_big, notes)
    except MemoryError:
        res.status, res.message = ERROR, "out of memory"
    res.seconds = time.monotonic() - t0
    return res
