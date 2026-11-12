"""Original-vs-compressed viewer: side by side, slider or flip, with shared zoom and pan.

Large sheets (up to ~55 MP) are drawn from a small pyramid of downscaled copies, so
zoomed-out views stay fast; at 100 % and above pixels are drawn unsmoothed, as they are.
Images are decoded in the background (a big sheet takes about a second) and the last few
are kept, so selecting rows never blocks the window.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, QSize, QThreadPool, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import worker

MODES = ("side", "slider", "flip")
CACHED_IMAGES = 4                        # decoded images kept; a 7500 px square sheet takes ~220 MB
QImageReader.setAllocationLimit(4096)   # MB; Qt's default 256 MB is too small for big sheets
_SKIPPED = object()                      # a load that was no longer wanted when its turn came


class _Pyramid:
    """An image plus lazily made 1/2, 1/4, ... copies."""

    def __init__(self, image: QImage) -> None:
        self.levels = {1.0: image}

    @property
    def size(self):
        return self.levels[1.0].size()

    def for_zoom(self, zoom: float) -> tuple[QImage, float]:
        """The smallest level that still has at least ``zoom`` resolution."""
        scale = 1.0
        while scale / 2 >= zoom and scale > 1 / 16:
            scale /= 2
        if scale not in self.levels:
            full = self.levels[1.0]
            self.levels[scale] = full.scaled(max(1, int(full.width() * scale)), max(1, int(full.height() * scale)),
                                             Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
        return self.levels[scale], scale


class CompareView(QWidget):
    zoom_changed = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(300, 200)
        self.images: list[_Pyramid | None] = [None, None]
        self.paths: list[str | None] = [None, None]
        self.labels = ["", ""]
        self.message = "Select a sheet"
        self.mode = "side"
        self.flipped = False          # flip mode: showing the original?
        self.split = 0.5              # slider position, fraction of width
        self.zoom = 1.0
        self.center = QPointF()       # image point at the centre of each viewport
        self._drag: tuple[str, QPointF] | None = None
        self._fitted: QSize | None = None                     # size of the image the view was last fitted to
        self._cache: OrderedDict[str, _Pyramid | None] = OrderedDict()   # None = couldn't be read
        self._loading: dict[str, worker.Job] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)                       # original and candidate in parallel

    # ------------------------------------------------------------------ public

    def set_images(self, original: str | None, candidate: str | None, labels: tuple[str, str],
                   message: str = "") -> None:
        """Show these files; ones not decoded yet appear as soon as they're loaded."""
        self.paths, self.labels, self.message = [original, candidate], list(labels), message
        self.images = [self._get(p) for p in self.paths]
        ref = self._reference()
        if ref is not None and ref.size != self._fitted:      # keep zoom/pan between sheets of one size
            self._fitted = ref.size
            self.fit()
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.update()

    def fit(self) -> None:
        ref = self._reference()
        if ref is None:
            return
        vw, vh = self._viewport_size()
        self._set_zoom(min(vw / ref.size.width(), vh / ref.size.height()))
        self.center = QPointF(ref.size.width() / 2, ref.size.height() / 2)
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self._set_zoom(zoom)
        self.update()

    def toggle_flip(self) -> None:
        self.flipped = not self.flipped
        self.update()

    # ------------------------------------------------------------------ helpers

    def _get(self, path: str | None) -> _Pyramid | None:
        """The decoded image if it's ready; otherwise start loading it and return None."""
        if not path:
            return None
        if path in self._cache:
            self._cache.move_to_end(path)
            return self._cache[path]
        if path not in self._loading:
            job = worker.start(lambda _job: self._read(path), self._pool)
            job.finished.connect(lambda result: self._loaded(path, result))
            job.failed.connect(lambda _tb: self._loaded(path, None))
            self._loading[path] = job
        return None

    def _read(self, path: str):
        """Worker thread: decode, unless the user has moved on to another sheet meanwhile."""
        if path not in self.paths:
            return _SKIPPED
        img = QImageReader(path).read()
        return None if img.isNull() else _Pyramid(img)

    def _loaded(self, path: str, result) -> None:
        del self._loading[path]
        if result is not _SKIPPED:
            self._cache[path] = result
            while len(self._cache) > CACHED_IMAGES:
                self._cache.popitem(last=False)
        if path in self.paths:
            self.set_images(*self.paths, tuple(self.labels), self.message)

    def _status(self, idx: int) -> str:
        path = self.paths[idx]
        return "loading..." if path in self._loading else "missing" if self.images[idx] is None else ""

    def _reference(self) -> _Pyramid | None:
        return self.images[0] or self.images[1]

    def _set_zoom(self, zoom: float) -> None:
        self.zoom = max(1 / 64, min(16.0, zoom))
        self.zoom_changed.emit(self.zoom)

    def _viewport_size(self) -> tuple[int, int]:
        w, h = max(1, self.width()), max(1, self.height())
        return (w // 2, h) if self.mode == "side" else (w, h)

    def _viewports(self) -> list[tuple[QRectF, int]]:
        """(rect, image index) pairs to draw."""
        w, h = self.width(), self.height()
        if self.mode == "side":
            return [(QRectF(0, 0, w // 2, h), 0), (QRectF(w // 2, 0, w - w // 2, h), 1)]
        if self.mode == "slider":
            x = w * self.split
            return [(QRectF(0, 0, x, h), 0), (QRectF(x, 0, w - x, h), 1)]
        return [(QRectF(0, 0, w, h), 0 if self.flipped else 1)]

    def _image_to_widget(self, frame: QRectF, p: QPointF) -> QPointF:
        """Map an image point into a viewport of size ``frame`` (slider halves share the full frame)."""
        return QPointF(frame.center().x() + (p.x() - self.center.x()) * self.zoom,
                       frame.center().y() + (p.y() - self.center.y()) * self.zoom)

    # ------------------------------------------------------------------ painting

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(40, 40, 40))
        if self._reference() is None:
            p.setPen(QColor(200, 200, 200))
            loading = any(path in self._loading for path in self.paths)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Loading..." if loading else self.message or "No image")
            return
        # In slider mode both halves are views of one full-width frame.
        frame_full = QRectF(self.rect())
        for rect, idx in self._viewports():
            frame = frame_full if self.mode == "slider" else rect
            p.save()
            p.setClipRect(rect)
            self._draw(p, self.images[idx], frame)
            status = self._status(idx)
            self._label(p, rect, f"{self.labels[idx]} ({status})" if status else self.labels[idx])
            p.restore()
        if self.mode == "side":
            p.setPen(QPen(QColor(0, 0, 0), 2))
            p.drawLine(self.width() // 2, 0, self.width() // 2, self.height())
        elif self.mode == "slider":
            x = self.width() * self.split
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        if self.message:
            self._label(p, QRectF(self.rect()), self.message, bottom=True)

    def _draw(self, p: QPainter, pyramid: _Pyramid | None, frame: QRectF) -> None:
        if pyramid is None:
            return
        img, scale = pyramid.for_zoom(self.zoom)
        size = pyramid.size
        top_left = self._image_to_widget(frame, QPointF(0, 0))
        target = QRectF(top_left.x(), top_left.y(), size.width() * self.zoom, size.height() * self.zoom)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1)
        p.drawImage(target, img, QRectF(0, 0, img.width(), img.height()))

    @staticmethod
    def _label(p: QPainter, rect: QRectF, text: str, bottom: bool = False) -> None:
        if not text:
            return
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 6
        x = rect.left() + 8
        y = rect.bottom() - h - 8 if bottom else rect.top() + 8
        p.fillRect(QRectF(x, y, w, h), QColor(0, 0, 0, 170))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(x, y, w, h), Qt.AlignmentFlag.AlignCenter, text)

    # ------------------------------------------------------------------ input

    def _near_split(self, pos: QPointF) -> bool:
        return self.mode == "slider" and abs(pos.x() - self.width() * self.split) < 8

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = ("split" if self._near_split(e.position()) else "pan", e.position())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        pos = e.position()
        if self._drag is None:
            self.setCursor(Qt.CursorShape.SplitHCursor if self._near_split(pos) else Qt.CursorShape.OpenHandCursor)
            return
        kind, last = self._drag
        if kind == "split":
            self.split = min(1.0, max(0.0, pos.x() / max(1, self.width())))
        else:
            self.center -= (pos - last) / self.zoom
        self._drag = (kind, pos)
        self.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def wheelEvent(self, e) -> None:  # noqa: N802
        if self._reference() is None:
            return
        factor = 1.25 if e.angleDelta().y() > 0 else 1 / 1.25
        # Keep the image point under the cursor fixed.
        pos = e.position()
        frame = next((r for r, _ in self._viewports() if r.contains(pos)), QRectF(self.rect()))
        if self.mode == "slider":
            frame = QRectF(self.rect())
        before = self.center + (pos - frame.center()) / self.zoom
        self._set_zoom(self.zoom * factor)
        self.center = before - (pos - frame.center()) / self.zoom
        self.update()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        key = e.key()
        if key == Qt.Key.Key_Space:
            self.toggle_flip()
        elif key in (Qt.Key.Key_0, Qt.Key.Key_F):
            self.fit()
        elif key == Qt.Key.Key_1:
            self.set_zoom(1.0)
        elif key == Qt.Key.Key_2:
            self.set_zoom(2.0)
        else:
            super().keyPressEvent(e)

    def resizeEvent(self, _e) -> None:  # noqa: N802
        self.update()
