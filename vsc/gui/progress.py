"""Modal progress window shown while a background job runs."""

from __future__ import annotations

import time


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget


class ProgressDialog(QDialog):
    stop_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.CustomizeWindowHint)
        self.setMinimumWidth(620)
        self.title = QLabel()
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.title.setFont(font)
        self.bar = QProgressBar()
        self.bar.setMinimumHeight(24)
        self.detail = QLabel()
        self.detail.setStyleSheet("color: gray")
        self.elapsed = QLabel()
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop after the current step")
        self.stop_button.clicked.connect(self._stop)

        bottom = QHBoxLayout()
        bottom.addWidget(self.elapsed)
        bottom.addStretch()
        bottom.addWidget(self.stop_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        layout.addLayout(bottom)

        self._t0 = 0.0
        self._step: tuple[str, float, int] | None = None   # label, start time, done at start
        self._done = self._total = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self, title: str, can_stop: bool = True) -> None:
        self.setWindowTitle(title)
        self.title.setText(title)
        self.bar.setRange(0, 0)            # busy indicator until real progress arrives
        self.bar.setFormat("")
        self.detail.setText("")
        self.stop_button.setEnabled(can_stop)
        self.stop_button.setText("Stop")
        self._t0 = time.monotonic()
        self._step, self._done, self._total = None, 0, 0
        self._tick()
        self._timer.start(1000)
        self.show()

    def set_progress(self, label: str, done: int, total: int) -> None:
        if self._step is None or self._step[0] != label:     # a new step: restart the estimate
            self._step = (label, time.monotonic(), done)
        self._done, self._total = done, total
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.bar.setFormat(f"{label}: %v / %m")

    def set_detail(self, text: str) -> None:
        text = text.strip()
        if text:
            width = max(200, self.detail.width())
            self.detail.setText(self.detail.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, width))

    def finish(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        text = f"Elapsed {_clock(time.monotonic() - self._t0)}"
        if (left := self._remaining()) is not None:
            text += f"   ·   {self._step[0]}: about {_clock(left)} left"
        self.elapsed.setText(text)

    def _remaining(self) -> float | None:
        """Seconds left in the current step, from its average pace so far (None until there's a pace)."""
        if self._step is None:
            return None
        _label, t0, done0 = self._step
        done, spent = self._done - done0, time.monotonic() - t0
        if done <= 0 or spent < 3 or self._done >= self._total:
            return None
        return (self._total - self._done) * spent / done

    def _stop(self) -> None:
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")
        self.stop_requested.emit()

    def reject(self) -> None:   # Esc: don't hide a running job
        pass
