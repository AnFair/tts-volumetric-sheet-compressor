"""Sheet table: a model over the project state, a filter/sort proxy, and thumbnails."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, QThreadPool, Qt, Signal
from PySide6.QtGui import QFont, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

from .. import report
from ..compress import COMPRESSED, FAILED, SKIPPED, candidate_name
from ..pipeline import PENDING, is_approved
from ..probe import OK
from ..units import format_size
from . import worker

Sheet = dict[str, Any]
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
THUMB_SIZE = 160


def _status(s: Sheet) -> str:
    return (s.get("compression") or {}).get("status", "")


def _format(s: Sheet) -> str:
    res = report.result_in_use(s)
    return candidate_name(res["format"], res["quality"]) if res else ""


def _new_size(s: Sheet) -> int | None:
    return report.result_in_use(s).get("size")


def _fmt_size(n: int | None) -> str:
    return format_size(n) if n else ""


def _new_url(s: Sheet) -> str:
    return (s.get("upload") or {}).get("url") or ""


@dataclass
class Column:
    title: str
    text: Callable[..., str]                     # (sheet) or, with by_threshold, (sheet, threshold)
    sort: Callable[..., Any]
    width: int = 80
    tip: str = ""
    link: Callable[[Sheet], str] | None = None   # makes the cell a clickable link to this URL
    by_threshold: bool = False


COLUMNS = [
    Column("", lambda s: "", lambda s: 0, 28, "Tick the sheets to compress"),
    Column("Preview", lambda s: "", lambda s: 0, 72, "Appears once the sheet is downloaded"),
    Column("Object", report.label, report.label, 180, "Name of the first object that uses this image"),
    Column("Size", lambda s: _fmt_size(report.size_of(s)), lambda s: report.size_of(s) or -1, 75,
           "Current file size online"),
    Column("Used by", lambda s: str(len(s["refs"])), lambda s: len(s["refs"]), 60,
           "How many places in the JSON (cards / decks, face or back) use this image"),
    Column("Scan", report.probe_text, report.probe_text, 110, "Result of checking the URL online"),
    Column("Result", _status, _status, 85, "Compression result; hover a row for details"),
    Column("Format", _format, _format, 75, "Format and quality of the compressed file"),
    Column("New Size", lambda s: _fmt_size(_new_size(s)), lambda s: _new_size(s) or -1, 75,
           "Size of the compressed file in use"),
    Column("Stage", report.final_status, report.final_status, 150,
           "Where the sheet stands: approval, export, upload, replaced in the mod", by_threshold=True),
    Column("URL", lambda s: s["key"], lambda s: s["key"], 300, "Current URL; click to open in the browser",
           link=lambda s: s["key"]),
    Column("New URL", _new_url, _new_url, 300, "URL of the uploaded file; click to open in the browser",
           link=_new_url),
]
CHECK_COL, PREVIEW_COL, SIZE_COL = 0, 1, 3


def row_tooltip(s: Sheet) -> str:
    comp = s.get("compression") or {}
    lines = [report.label(s), report.probe_text(s)]
    if comp:
        lines.append(f"{comp['status']}: {comp.get('message') or 'ok'}")
        if (c := comp.get("chosen")) and not report.result_in_use(s):
            lines.append(f"best try (not used): {candidate_name(c['format'], c['quality'])}, "
                         f"{format_size(c['size'])}, score {c['score']}")
    return "\n".join(lines)


class Thumbnails(QObject):
    """Small previews of downloaded originals, cached on disk and made in the background."""

    ready = Signal(str)   # sheet key

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.folder = folder
        self._pixmaps: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._jobs: list[worker.Job] = []
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    def get(self, sheet: Sheet) -> QPixmap | None:
        key = sheet["key"]
        if key in self._pixmaps:
            return self._pixmaps[key]
        source = (sheet.get("download") or {}).get("path")
        if not source or not Path(source).exists():
            return None
        thumb = self.folder / (Path(source).stem + ".jpg")
        if thumb.exists():
            self._pixmaps[key] = QPixmap(str(thumb))
            return self._pixmaps[key]
        if key not in self._pending:
            self._pending.add(key)
            job = worker.start(lambda _job: _make_thumb(Path(source), thumb), self._pool)
            job.finished.connect(lambda _r, k=key, j=job: self._done(k, j))
            job.failed.connect(lambda _e, k=key, j=job: self._done(k, j))
            self._jobs.append(job)
        return None

    def _done(self, key: str, job: worker.Job) -> None:
        self._pending.discard(key)
        self._jobs.remove(job)
        self.ready.emit(key)


def _make_thumb(source: Path, thumb: Path) -> None:
    thumb.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as im:
        im.draft("RGB", (THUMB_SIZE * 4, THUMB_SIZE * 4))   # fast JPEG decode at reduced size
        im = im.convert("RGB")
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        im.save(thumb, "JPEG", quality=85)


class SheetModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.sheets: list[Sheet] = []
        self.threshold = 0
        self.checked: set[str] = set()
        self.thumbnails: Thumbnails | None = None
        self._rows: dict[str, int] = {}

    def set_state(self, state: dict[str, Any], thumbnails: Thumbnails) -> None:
        self.beginResetModel()
        if self.thumbnails is not thumbnails:
            if self.thumbnails:
                self.thumbnails.ready.disconnect(self.refresh)
            self.thumbnails = thumbnails
            thumbnails.ready.connect(self.refresh)
        self.sheets = state.get("sheets", [])
        self.threshold = state.get("threshold", 0)
        self._rows = {s["key"]: i for i, s in enumerate(self.sheets)}
        self.checked &= set(self._rows)
        self.endResetModel()

    def sheet(self, row: int) -> Sheet:
        return self.sheets[row]

    def refresh(self, key: str) -> None:
        if (row := self._rows.get(key)) is not None:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def refresh_all(self) -> None:
        if self.sheets:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.sheets) - 1, len(COLUMNS) - 1))

    def set_checked(self, keys: set[str]) -> None:
        self.checked = keys & set(self._rows)
        if self.sheets:
            self.dataChanged.emit(self.index(0, CHECK_COL), self.index(len(self.sheets) - 1, CHECK_COL))

    # Qt model API -----------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.sheets)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section].title
            if role == Qt.ItemDataRole.ToolTipRole:
                return COLUMNS[section].tip or None
        return None

    def flags(self, index):
        f = super().flags(index)
        return f | Qt.ItemFlag.ItemIsUserCheckable if index.column() == CHECK_COL else f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        s, col = self.sheets[index.row()], index.column()
        column = COLUMNS[col]
        extra = (self.threshold,) if column.by_threshold else ()
        if role == Qt.ItemDataRole.DisplayRole:
            return column.text(s, *extra)
        if role == SORT_ROLE:
            return column.sort(s, *extra)
        if role == Qt.ItemDataRole.CheckStateRole and col == CHECK_COL:
            return Qt.CheckState.Checked if s["key"] in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole and col == PREVIEW_COL and self.thumbnails:
            return self.thumbnails.get(s)
        is_link = COLUMNS[col].link is not None
        if role == Qt.ItemDataRole.ToolTipRole:
            return "Click to open in the browser" if is_link else row_tooltip(s)
        if is_link and role == Qt.ItemDataRole.ForegroundRole:
            return QApplication.palette().color(QPalette.ColorRole.Link)
        if is_link and role == Qt.ItemDataRole.FontRole:
            font = QFont()
            font.setUnderline(True)
            return font
        return None

    def link(self, row: int, col: int) -> str:
        """URL behind a link cell ('' if the cell isn't a link or is empty)."""
        column = COLUMNS[col]
        return column.link(self.sheets[row]) if column.link else ""

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == CHECK_COL:
            key = self.sheets[index.row()]["key"]
            if Qt.CheckState(value) == Qt.CheckState.Checked:
                self.checked.add(key)
            else:
                self.checked.discard(key)
            self.dataChanged.emit(index, index)
            return True
        return False


# Filters: name -> predicate(sheet, threshold)
FILTERS: dict[str, Callable[[Sheet, int], bool]] = {
    "All sheets": lambda s, t: True,
    "Over threshold": lambda s, t: bool(report.over_threshold(s, t)),
    "Compressed": lambda s, t: (s.get("compression") or {}).get("status") == COMPRESSED,
    "Pending review": lambda s, t: s.get("approval") == PENDING,
    "Approved": lambda s, t: is_approved(s),
    "Skipped / failed": lambda s, t: (s.get("compression") or {}).get("status") in (SKIPPED, FAILED),
    "Exported, not uploaded": lambda s, t: bool(s.get("output")) and not _new_url(s),
    "Uploaded, not applied": lambda s, t: bool(_new_url(s)) and not (s.get("upload") or {}).get("applied"),
    "Replaced": lambda s, t: bool((s.get("upload") or {}).get("applied")),
    "Problems": lambda s, t: report.probe_status(s) not in (OK, "not-probed")
                             or (s.get("download") or {}).get("status", "ok") != "ok",
}


class SheetFilter(QSortFilterProxyModel):
    def __init__(self, source: SheetModel) -> None:
        super().__init__()
        self.setSourceModel(source)
        self.setSortRole(SORT_ROLE)
        self.mode = "All sheets"
        self.text = ""

    def set_filter(self, mode: str | None = None, text: str | None = None) -> None:
        self.beginFilterChange()
        self.mode = self.mode if mode is None else mode
        self.text = self.text if text is None else text.lower()
        self.endFilterChange()

    def filterAcceptsRow(self, row, parent) -> bool:  # noqa: N802
        model: SheetModel = self.sourceModel()
        s = model.sheet(row)
        if not FILTERS[self.mode](s, model.threshold):
            return False
        return not self.text or self.text in report.label(s).lower() or self.text in s["key"].lower()

    def visible_keys(self) -> list[str]:
        model: SheetModel = self.sourceModel()
        return [model.sheet(self.mapToSource(self.index(r, 0)).row())["key"] for r in range(self.rowCount())]
