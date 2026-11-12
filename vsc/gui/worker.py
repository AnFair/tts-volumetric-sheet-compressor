"""Run library calls off the UI thread; their callbacks arrive in the UI thread as Qt signals."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class Job(QObject):
    """Handle for one background call. Pass its methods as the library's callbacks."""

    log = Signal(str)
    progress = Signal(str, int, int)      # label, done, total
    sheet_done = Signal(str)              # sheet key
    finished = Signal(object)             # return value
    failed = Signal(str)                  # traceback text

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def progress_for(self, label: str) -> Callable[[int, int], None]:
        return lambda done, total: self.progress.emit(label, done, total)


class _Runnable(QRunnable):
    def __init__(self, fn: Callable[[Job], Any], job: Job) -> None:
        super().__init__()
        self.fn, self.job = fn, job

    def run(self) -> None:
        try:
            self.job.finished.emit(self.fn(self.job))
        except Exception:  # noqa: BLE001 - surface anything to the UI
            self.job.failed.emit(traceback.format_exc())


def start(fn: Callable[[Job], Any], pool: QThreadPool | None = None) -> Job:
    """Run ``fn(job)`` in a worker thread. Keep a reference to the returned job while it runs."""
    job = Job()
    (pool or QThreadPool.globalInstance()).start(_Runnable(fn, job))
    return job
