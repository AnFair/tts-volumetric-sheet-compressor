"""Tiled SSIMULACRA2 scoring.

A float32, array-based port of SSIMULACRA2 that reuses the weights from the
``ssimulacra2`` package (BSD-3-Clause, (c) 2025 Pacidus), which follows the
libjxl reference implementation.

The sheet's score is the *worst tile* score, so one text-heavy card degrading
can't be hidden by many easy ones.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy import ndimage
from ssimulacra2.ssimulacra2 import weights as _WEIGHTS

_NUM_SCALES = 6
_C2 = np.float32(0.0009)
_OPSIN = np.array(
    [[0.30, 1.0 - 0.078 - 0.30, 0.078],
     [0.23, 1.0 - 0.078 - 0.23, 0.078],
     [0.24342268924547819, 0.20476744424496821, 1.0 - 0.24342268924547819 - 0.20476744424496821]],
    dtype=np.float32,
)
_BIAS = np.float32(0.0037930732552754493)
_CBRT_BIAS = np.float32(np.cbrt(0.0037930732552754493))

# sRGB -> linear lookup table for uint8 input
_SRGB_LUT = np.arange(256, dtype=np.float64) / 255.0
_SRGB_LUT = np.where(_SRGB_LUT <= 0.04045, _SRGB_LUT / 12.92, ((_SRGB_LUT + 0.055) / 1.055) ** 2.4).astype(np.float32)

TILE = 512


# --------------------------------------------------------------------------- core


def to_linear(rgb_u8: np.ndarray) -> np.ndarray:
    return _SRGB_LUT[rgb_u8]


def _xyb(lin: np.ndarray) -> np.ndarray:
    mixed = lin @ _OPSIN.T + _BIAS
    np.maximum(mixed, 0, out=mixed)
    mixed = np.cbrt(mixed) - _CBRT_BIAS
    out = np.empty_like(mixed)
    out[..., 0] = 0.5 * (mixed[..., 0] - mixed[..., 1]) * 14.0 + 0.42
    out[..., 1] = 0.5 * (mixed[..., 0] + mixed[..., 1]) + 0.01
    out[..., 2] = mixed[..., 2] - out[..., 1] + 0.01 + 0.55   # (B - Y) + 0.55, Y before its +0.01
    return out


def _blur(img: np.ndarray) -> np.ndarray:
    # sigma 1.5; zero boundary vertically, mirrored horizontally (as the reference port).
    return ndimage.gaussian_filter(img, sigma=1.5, truncate=3.33, mode=("constant", "reflect"), axes=(0, 1))


def _down2(lin: np.ndarray) -> np.ndarray:
    h, w, _ = lin.shape
    if h % 2 or w % 2:
        lin = np.pad(lin, ((0, h % 2), (0, w % 2), (0, 0)), mode="edge")
    h, w, c = lin.shape
    return lin.reshape(h // 2, 2, w // 2, 2, c).mean(axis=(1, 3), dtype=np.float32)


@dataclass
class _RefScale:
    x: np.ndarray
    mu: np.ndarray
    s11: np.ndarray


@dataclass
class Reference:
    """Precomputed original-side data for one tile (reused across candidates)."""

    scales: list[_RefScale] = field(default_factory=list)


def prepare(lin: np.ndarray) -> Reference:
    ref = Reference()
    for scale in range(_NUM_SCALES):
        if lin.shape[0] < 8 or lin.shape[1] < 8:
            break
        x = _xyb(lin)
        ref.scales.append(_RefScale(x, _blur(x), _blur(x * x)))
        if scale < _NUM_SCALES - 1:
            lin = _down2(lin)
    return ref


def score(ref: Reference, lin2: np.ndarray) -> float:
    """SSIMULACRA2 of a distorted tile (linear RGB) against a prepared reference."""
    feats = np.zeros((3, len(ref.scales), 6), dtype=np.float64)   # [channel, scale, (ssim1, ring1, blur1, ssim4, ring4, blur4)]
    for si, rs in enumerate(ref.scales):
        y = _xyb(lin2)
        mu2 = _blur(y)
        s22 = _blur(y * y)
        s12 = _blur(rs.x * y)

        m1, m2 = rs.mu, mu2
        num_m = 1.0 - (m1 - m2) ** 2
        num_s = 2.0 * (s12 - m1 * m2) + _C2
        den_s = (rs.s11 - m1 * m1) + (s22 - m2 * m2) + _C2
        d = np.maximum(1.0 - num_m * num_s / den_s, 0.0)

        ed = (1.0 + np.abs(y - mu2)) / (1.0 + np.abs(rs.x - m1)) - 1.0
        ring = np.maximum(ed, 0.0)
        blur = np.maximum(-ed, 0.0)

        for k, mp in ((0, d), (1, ring), (2, blur)):
            feats[:, si, k] = mp.mean(axis=(0, 1), dtype=np.float64)
            feats[:, si, k + 3] = (mp.astype(np.float64) ** 4).mean(axis=(0, 1)) ** 0.25

        if si < len(ref.scales) - 1:
            lin2 = _down2(lin2)

    # Weight order: channel -> scale -> norm(1, 4) -> (ssim, ringing, blurring)
    total = 0.0
    i = 0
    for c in range(3):
        for si in range(len(ref.scales)):
            for n in (0, 3):
                for k in range(3):
                    total += _WEIGHTS[i] * abs(feats[c, si, n + k])
                    i += 1
    total *= 0.9562382616834844
    total = 2.326765642916932 * total - 0.020884521182843837 * total**2 + 6.248496625763138e-05 * total**3
    return 100.0 - 10.0 * total**0.6276336467831387 if total > 0 else 100.0


# --------------------------------------------------------------------------- tiles


def tile_grid(width: int, height: int, tile: int = TILE) -> list[tuple[int, int, int, int]]:
    """Boxes (x0, y0, x1, y1); a remainder smaller than half a tile merges into its neighbour."""

    def cuts(n: int) -> list[int]:
        edges = list(range(0, n, tile)) + [n]
        if len(edges) > 2 and edges[-1] - edges[-2] < tile // 2:
            edges.pop(-2)
        return edges

    xs, ys = cuts(width), cuts(height)
    return [(xs[i], ys[j], xs[i + 1], ys[j + 1]) for j in range(len(ys) - 1) for i in range(len(xs) - 1)]


def _composite(rgba_u8: np.ndarray, bg: float) -> np.ndarray:
    lin = to_linear(rgba_u8[..., :3])
    a = rgba_u8[..., 3:4].astype(np.float32) / 255.0
    return lin * a + np.float32(bg) * (1.0 - a)


# For transparent images, score against a dark and a light background and keep the worse.
_ALPHA_BGS = (0.1, 0.9)


class TileScorer:
    """Scores candidates against one original, tile by tile, caching original-side work."""

    def __init__(self, original: np.ndarray):
        if original.ndim != 3 or original.shape[2] not in (3, 4):
            raise ValueError("expected an RGB or RGBA uint8 array")
        self.original = original
        self.alpha = original.shape[2] == 4
        h, w = original.shape[:2]
        self.tiles = tile_grid(w, h)
        self._cache: dict[int, list[Reference]] = {}

    def _views(self, img: np.ndarray, box) -> list[np.ndarray]:
        x0, y0, x1, y1 = box
        t = img[y0:y1, x0:x1]
        if self.alpha:
            return [_composite(t, bg) for bg in _ALPHA_BGS]
        return [to_linear(t)]

    def _refs(self, idx: int, keep: bool) -> list[Reference]:
        refs = self._cache.get(idx)
        if refs is None:
            refs = [prepare(v) for v in self._views(self.original, self.tiles[idx])]
            if keep:
                self._cache[idx] = refs
        return refs

    def _one(self, candidate: np.ndarray, i: int, cache: bool) -> float:
        refs = self._refs(i, cache)
        views = self._views(candidate, self.tiles[i])
        return float(min(score(r, v) for r, v in zip(refs, views)))

    def scores(
        self,
        candidate: np.ndarray,
        indices: Sequence[int] | None = None,
        cache: bool = False,
        workers: int = 1,
    ) -> dict[int, float]:
        """Per-tile scores for ``indices`` (default: all). ``cache`` keeps the original side."""
        if candidate.shape != self.original.shape:
            raise ValueError(f"shape mismatch {candidate.shape} vs {self.original.shape}")
        idx = list(range(len(self.tiles)) if indices is None else indices)
        if workers <= 1 or len(idx) < 2:
            return {i: self._one(candidate, i, cache) for i in idx}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            vals = pool.map(lambda i: self._one(candidate, i, cache), idx)
            return dict(zip(idx, vals))
