"""Main window: scan, pick sheets, compress, review, export."""

from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIntValidator, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QSplitter, QTableView, QTabWidget, QTextBrowser,
    QToolBar, QVBoxLayout, QWidget,
)

from .. import mapping, pipeline, report, urls
from ..compress import FIRST, FIXED, SMALLEST, Settings, candidate_name
from ..project import PROJECTS_DIR, Project, project_path
from ..units import format_size
from . import worker
from .compare import MODES, CompareView
from .model import COLUMNS, FILTERS, SIZE_COL, SheetFilter, SheetModel, Thumbnails
from .progress import ProgressDialog

# Labels and tooltips for the fixed / override formats (ids from compress.FIXED_FORMATS)
FORMAT_LABELS = {
    "jpeg": ("JPEG", "JPEG at the chosen quality, full colour resolution (4:4:4)"),
    "webp": ("WebP", "Lossy WebP at the chosen quality; supports transparency"),
    "jpeg-keep": ("JPEG, keep source quality",
                  "Only for JPEG originals: re-save with the original's own compression settings (almost lossless)"),
}
MODE_LABELS = {
    FIRST: ("First that passes", "Try formats in priority order; keep the first that meets the target and saves "
                                 "enough. Fastest."),
    SMALLEST: ("Smallest file", "Try every format; keep the smallest that meets the target. Slower."),
    FIXED: ("Fixed format/quality", "Encode every sheet with the format and quality chosen here; still scored."),
}
ORDERS = {"JPEG, then WebP": ("jpeg", "webp", "png"), "WebP, then JPEG": ("webp", "jpeg", "png")}
KB = 1024
# Gap between toolbar items and outer padding; a stylesheet hides the native dividers, so draw them here.
TOOLBAR_STYLE = ("QToolBar { spacing: 8px; padding: 4px 4px; }"
                 "QToolBar::separator { width: 1px; margin: 3px 4px; border-left: 1px solid palette(mid); }")
DEFAULT_THRESHOLD = 2048 * KB
TARGET_TIP = ("Quality target: the worst 512 px tile must score at least this (SSIMULACRA2).\n"
              "90 = no difference when flipping at 100% zoom\n85 = none when flipping at normal distance\n"
              "80 = none side by side (recommended)\n70 = high quality, small artifacts visible up close")


def _combo(items: dict[str, tuple[str, str]]) -> QComboBox:
    """Combo box from {id: (label, tooltip)}; currentData() is the id."""
    combo = QComboBox()
    for key, (label, tip) in items.items():
        combo.addItem(label, key)
        combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
    return combo


class IntField(QLineEdit):
    """Text box for a whole number from ``lo`` to ``hi``. Empty or out-of-range input reverts to the
    last valid value when the box loses focus; ``committed`` fires when a valid number is entered."""

    committed = Signal(int)

    def __init__(self, lo: int, hi: int, value: int, width: int, tip: str = "") -> None:
        super().__init__(str(value))
        self._value = value
        self.setValidator(QIntValidator(lo, hi, self))
        self.setFixedWidth(width)
        self.setToolTip(tip)
        self.editingFinished.connect(self._commit)   # only fires for valid input

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:  # noqa: N802
        self._value = value
        self.setText(str(value))

    def _commit(self) -> None:
        self.setValue(int(self.text()))
        self.committed.emit(self._value)

    def focusOutEvent(self, e) -> None:  # noqa: N802
        if not self.hasAcceptableInput():
            self.setText(str(self._value))
        super().focusOutEvent(e)


def _labeled(text: str, field: QWidget) -> QWidget:
    """``field`` with a caption in front, as one toolbar / layout item."""
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(QLabel(text))
    row.addWidget(field)
    box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)   # don't take the bar's spare room
    return box


def _quality_box(value: int) -> IntField:
    return IntField(1, 100, value, 45, "Encoder quality, 1-100 (not the Target Quality score)")


class ScanDialog(QDialog):
    """Inputs and excludes for a scan."""

    def __init__(self, parent: QWidget, state: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scan JSON files")
        self.resize(640, 380)
        self.paths = QListWidget()
        self.paths.addItems(state.get("inputs", []))
        add_folder, add_files, remove = QPushButton("Add folder..."), QPushButton("Add files..."), QPushButton("Remove")
        add_folder.clicked.connect(self._add_folder)
        add_files.clicked.connect(self._add_files)
        remove.clicked.connect(lambda: [self.paths.takeItem(self.paths.row(i)) for i in self.paths.selectedItems()])
        buttons = QHBoxLayout()
        for b in (add_folder, add_files, remove):
            buttons.addWidget(b)
        buttons.addStretch()

        self.exclude = QLineEdit("; ".join(state.get("exclude", [])))
        self.exclude.setPlaceholderText("globs separated by ';', e.g. */downloadable/*")
        self.refresh = QCheckBox("Re-check sizes that are already known")

        form = QFormLayout()
        form.addRow("Exclude", self.exclude)
        form.addRow("", self.refresh)
        ok = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok.accepted.connect(self._accept)
        ok.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("JSON files or folders to scan:"))
        layout.addWidget(self.paths)
        layout.addLayout(buttons)
        layout.addLayout(form)
        layout.addWidget(ok)

    def _add_folder(self) -> None:
        if d := QFileDialog.getExistingDirectory(self, "Folder to scan"):
            self.paths.addItem(d)

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "JSON files", filter="JSON (*.json)")
        self.paths.addItems(files)

    def _accept(self) -> None:
        if not self.paths.count():
            QMessageBox.warning(self, "Scan", "Add at least one file or folder.")
            return
        self.accept()

    def values(self) -> dict[str, Any]:
        return {
            "paths": [self.paths.item(i).text() for i in range(self.paths.count())],
            "exclude": [x.strip() for x in self.exclude.text().split(";") if x.strip()],
            "refresh": self.refresh.isChecked(),
        }


class MapDialog(QDialog):
    """Where the new URLs come from: TTS's upload list (CloudInfo.bson, path shown and changeable) or pasted URLs.

    After exec(): ``data`` holds the bytes to search and ``path`` the file used (None when pasted).
    """

    def __init__(self, parent: QWidget, remembered: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Map uploads")
        self.resize(760, 400)
        self.data, self.path = b"", None

        self.use_file = QRadioButton("TTS upload list (CloudInfo.bson)")
        self.use_paste = QRadioButton("URLs I paste")
        self.use_file.setChecked(True)
        detected = mapping.find_cloudinfo()                    # most recently changed first
        self.file = QComboBox()
        self.file.setEditable(True)
        self.file.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for p in dict.fromkeys(([remembered] if remembered and Path(remembered).exists() else [])
                               + [str(p) for p in detected]):
            self.file.addItem(p)
            changed = datetime.fromtimestamp(Path(p).stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self.file.setItemData(self.file.count() - 1, f"Last changed {changed}", Qt.ItemDataRole.ToolTipRole)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        hint = ("TTS keeps its upload list in your Steam folder:\n" + mapping.cloudinfo_hint()
                + f"\n({mapping.TTS_APP_ID} is Tabletop Simulator; the account number is a folder named after your "
                "Steam ID. With one Steam account there is only one.)")
        if len(detected) > 1:
            hint += "\nSeveral Steam accounts have one: pick the account you uploaded with (newest is listed first)."
        elif not detected:
            hint += "\nIt wasn't found automatically: use Browse."
        self.hint = QLabel(hint)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: gray")
        self.hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Paste the URLs of your uploaded files, in any order. Each is matched to its "
                                     "sheet by the file fingerprint inside the URL.")
        file_row = QHBoxLayout()
        file_row.addWidget(self.file, 1)
        file_row.addWidget(browse)
        self._file_widgets = (self.file, browse, self.hint)
        self.use_file.toggled.connect(self._mode_changed)
        self._mode_changed()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Find the new URLs of the files you uploaded from out/ in:"))
        layout.addWidget(self.use_file)
        layout.addLayout(file_row)
        layout.addWidget(self.hint)
        layout.addSpacing(8)
        layout.addWidget(self.use_paste)
        layout.addWidget(self.text, 1)
        layout.addWidget(buttons)

    def _mode_changed(self) -> None:
        for w in self._file_widgets:
            w.setEnabled(self.use_file.isChecked())
        self.text.setEnabled(self.use_paste.isChecked())

    def _browse(self) -> None:
        current = Path(self.file.currentText().strip())
        start = current.parent if current.parent.is_dir() and current.name else mapping.steam_userdata() or Path.home()
        path, _ = QFileDialog.getOpenFileName(self, "TTS upload list", str(start),
                                              "TTS upload list (CloudInfo.bson);;All files (*)")
        if path:
            self.file.setCurrentText(str(Path(path)))

    def _accept(self) -> None:
        if self.use_file.isChecked():
            path = Path(self.file.currentText().strip())
            if not path.is_file():
                QMessageBox.warning(self, "Map uploads", f"File not found:\n{path}")
                return
            data = path.read_bytes()
            if not urls.steam_urls_by_sha1(data):
                QMessageBox.warning(self, "Map uploads", "This file contains no Steam upload URLs. Pick "
                                    f"CloudInfo.bson in {mapping.cloudinfo_hint()}")
                return
            self.data, self.path = data, str(path)
        else:
            data = self.text.toPlainText().encode("utf-8")
            if not urls.steam_urls_by_sha1(data):
                QMessageBox.warning(self, "Map uploads", "No Steam upload URLs found in the pasted text.")
                return
            self.data, self.path = data, None
        self.accept()


def _link(url: str, text: str | None = None) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(text or url)}</a>'


def details_html(s: dict[str, Any]) -> str:
    """Everything known about one sheet, with clickable URL and files."""
    out = [f"<b>{html.escape(report.label(s))}</b><br>URL: {_link(s['key'])}"]
    out += [f"<br>&nbsp;&nbsp;also written as: {html.escape(v)}" for v in s.get("variants", []) if v != s["key"]]
    out.append(f"<br>Scan: {html.escape(report.probe_text(s))} "
               f"{format_size(report.size_of(s)) if report.size_of(s) else ''}")
    if comp := s.get("compression"):
        src = comp.get("source") or {}
        out.append(f"<br>Source: {src.get('width')}x{src.get('height')} {src.get('format')} {src.get('mode')}"
                   f"{' with transparency' if src.get('alpha_used') else ''}")
        out.append(f"<br>Result: <b>{comp['status']}</b> {html.escape(comp.get('message') or '')} "
                   f"({comp.get('seconds')} s)")
        for c in comp.get("candidates", []):
            score = "lossless" if c["score"] is None else f"worst tile {c['score']}"
            out.append(f"<br>&nbsp;&nbsp;tried {candidate_name(c['format'], c['quality'])}: "
                       f"{format_size(c['size'])}, {score}{' (meets target)' if c['passed'] else ''}")
    out.append(f"<br>Approval: {s.get('approval') or '-'} &nbsp; In out/: {html.escape(s.get('output') or '-')}")
    out.append(f"<br>Used by: {len(s['refs'])} (see reports/references.csv)")
    return "".join(out)


class MainWindow(QMainWindow):
    def __init__(self, project_dir: str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Volumetric Sheet Compressor")
        self.resize(1500, 900)
        self.settings_store = QSettings()   # organisation/app names are set in main()
        self.project: Project | None = None
        self.state: dict[str, Any] = {}
        self.job: worker.Job | None = None
        self.thumbnails: Thumbnails | None = None

        self.model = SheetModel()
        self.proxy = SheetFilter(self.model)
        self.progress = ProgressDialog(self)
        self.progress.stop_requested.connect(self.stop)
        self._build_ui()
        # Reopen the last project only if it still exists, so a deleted one isn't silently recreated.
        last = self.settings_store.value("project") or ""
        self.open_project(project_dir or (last if last and Project(last).state_path.exists() else ""))

    # ------------------------------------------------------------------ UI

    def _action(self, text: str, slot: Callable, shortcut: str | None = None, tip: str = "") -> QAction:
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setToolTip(tip or text)
        return a

    def _build_ui(self) -> None:
        self.act_new = self._action("New project...", self._new_project, "Ctrl+N")
        self.act_open = self._action("Open project...", self._choose_project, "Ctrl+O")
        self.act_scan = self._action("Scan...", self.scan, "Ctrl+R", "Find decksheets in JSON files and check sizes")
        self.act_download = self._action("Download checked", self.download, "Ctrl+D",
                                         "Download the originals of the ticked sheets, to preview them before compressing")
        self.act_compress = self._action("Compress checked", self.compress, "Ctrl+Return",
                                         "Compress the ticked sheets (downloads them first if needed)")
        self.act_export = self._action("Export", self.export, "Ctrl+E",
                                       "Copy approved files to out/ (removing unapproved ones) and write the reports")
        self.act_approve = self._action("Approve", lambda: self._set_approval(pipeline.APPROVED), "A",
                                        "Mark the selected sheets for export")
        self.act_reject = self._action("Reject", lambda: self._set_approval(pipeline.REJECTED), "R",
                                       "Keep the selected sheets out of the export")
        self.act_reencode = self._action("Re-encode", self.reencode, tip="Encode the selected sheet with these settings")
        self.act_map = self._action("Map uploads", self.map_uploads, "Ctrl+M",
                                    "Find the new URLs of the files you uploaded through TTS (from its upload list or pasted URLs)")
        self.act_apply = self._action("Apply...", self.apply, "Ctrl+Shift+A",
                                      "Replace old URLs with the new ones in the mod's files (preview first, backups kept)")
        self.act_undo = self._action("Undo last apply", self.undo_apply, tip="Restore the files the last Apply changed")
        open_out = self._action("Open out folder", lambda: self._open_folder("out_dir"))
        open_reports = self._action("Open reports folder", lambda: self._open_folder("reports_dir"))

        menu = self.menuBar().addMenu("&File")
        for a in (self.act_new, self.act_open, self.act_scan, self.act_export, self.act_map,
                  self.act_apply, self.act_undo, open_out, open_reports):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self._action("Quit", self.close, "Ctrl+Q"))

        # Main toolbar
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setStyleSheet(TOOLBAR_STYLE)
        self.addToolBar(tb)
        self.mode = _combo(MODE_LABELS)
        self.mode.setToolTip("How to pick the compression method")
        self.target = IntField(1, 100, int(Settings().target), 45, TARGET_TIP)
        self.order = QComboBox()
        self.order.addItems(list(ORDERS))
        self.order.setToolTip("Priority: which format is tried first (JPEG is skipped for transparent sheets)")
        self.fixed_format = _combo({f: FORMAT_LABELS[f] for f in FORMAT_LABELS})
        self.fixed_quality = _quality_box(85)
        self.auto_approve = QCheckBox("Auto-approve")
        self.auto_approve.setChecked(True)
        self.auto_approve.setToolTip("Mark results that meet the target as approved straight away")
        # Compression settings first, then the steps in workflow order, one divider between each.
        tb.addWidget(self.mode)
        tb.addWidget(_labeled("Target Quality", self.target))
        self._order_action = tb.addWidget(self.order)
        self._fixed_actions = [tb.addWidget(self.fixed_format), tb.addWidget(self.fixed_quality)]
        tb.addWidget(self.auto_approve)
        self.mode.currentIndexChanged.connect(lambda _i: self._mode_changed())
        self.fixed_format.currentIndexChanged.connect(
            lambda _i: self.fixed_quality.setEnabled(self.fixed_format.currentData() != "jpeg-keep"))
        self._mode_changed()
        steps = (self.act_new, self.act_scan, self.act_download, self.act_compress, self.act_export, self.act_map,
                 self.act_apply)
        for n, step in enumerate(steps, 1):
            step.setIconText(f"{n}. {step.text()}")   # the toolbar shows the numbered label, the menu the plain one
            tb.addSeparator()
            tb.addAction(step)

        # Review bar (acts on the selected sheets), shown above the compare view
        review = QToolBar("Review")
        review.setStyleSheet(TOOLBAR_STYLE)
        review.addAction(self.act_approve)
        review.addAction(self.act_reject)
        review.addSeparator()
        self.re_format = _combo(FORMAT_LABELS)
        self.re_quality = _quality_box(90)
        self.re_format.currentIndexChanged.connect(
            lambda _i: self.re_quality.setEnabled(self.re_format.currentData() != "jpeg-keep"))
        for w in (QLabel("Override"), self.re_format, self.re_quality):
            review.addWidget(w)
        review.addAction(self.act_reencode)

        # Filter bar + table
        self.filter_mode = QComboBox()
        self.filter_mode.addItems(list(FILTERS))
        self.filter_mode.currentTextChanged.connect(lambda m: self.proxy.set_filter(mode=m))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name or URL")
        self.search.textChanged.connect(lambda t: self.proxy.set_filter(text=t))
        self.threshold = IntField(1, 10_000_000, DEFAULT_THRESHOLD // KB, 70,
                                  "Size threshold in kilobytes (2048 = 2 MB). Used by the 'Over threshold' filter, "
                                  "'Check over threshold', Stage and the report; compression only ever handles "
                                  "the sheets you tick.")
        self.threshold.committed.connect(self._threshold_changed)
        check_over = QPushButton("Check over threshold")
        check_over.clicked.connect(self._check_over_threshold)
        check_visible = QPushButton("Check filtered")
        check_visible.clicked.connect(lambda: self._set_checked(self.model.checked | set(self.proxy.visible_keys())))
        uncheck = QPushButton("Uncheck all")
        uncheck.clicked.connect(lambda: self._set_checked(set()))
        bar = QHBoxLayout()
        for w in (self.filter_mode, self.search, _labeled("Threshold (KB)", self.threshold), check_over, check_visible,
                  uncheck):
            bar.addWidget(w)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(SIZE_COL, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        row_h = int(self.table.fontMetrics().height() * 2.5) + 4    # ~2.5 lines of text
        self.table.verticalHeader().setDefaultSectionSize(row_h)
        self.table.verticalHeader().hide()
        self.table.setIconSize(QSize(int((row_h - 4) * 1.6), row_h - 4))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, c in enumerate(COLUMNS):
            self.table.setColumnWidth(col, c.width)
        self.table.selectionModel().currentRowChanged.connect(lambda *_: self._show_current())
        self.table.clicked.connect(self._on_table_clicked)

        self.table.setAlternatingRowColors(True)
        palette = self.table.palette()   # the default alternate colour is barely visible: make it a bit darker
        palette.setColor(QPalette.ColorRole.AlternateBase, palette.color(QPalette.ColorRole.Base).darker(108))
        self.table.setPalette(palette)
        # Header: bold, a darker tint than any row and a strong bottom line, so it never reads as a row.
        header_bg = palette.color(QPalette.ColorRole.Base).darker(125).name()
        header.setStyleSheet(
            f"QHeaderView::section {{ background: {header_bg}; font-weight: bold; padding: 4px 6px;"
            " border: none; border-right: 1px solid palette(mid); border-bottom: 2px solid palette(dark); }")

        sheets_table = QWidget()
        lv = QVBoxLayout(sheets_table)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addLayout(bar)
        lv.addWidget(self.table)

        # Right: compare / details / log
        self.compare = CompareView()
        mode_bar = QHBoxLayout()
        group = QButtonGroup(self)
        for mode, text in zip(MODES, ("Side by side", "Slider", "Flip (Space)")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(mode == self.compare.mode)
            b.clicked.connect(lambda _=False, m=mode: self.compare.set_mode(m))
            group.addButton(b)
            mode_bar.addWidget(b)
        mode_bar.addStretch()
        for text, slot in (("Fit", self.compare.fit), ("100%", lambda: self.compare.set_zoom(1.0)),
                           ("200%", lambda: self.compare.set_zoom(2.0))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            mode_bar.addWidget(b)
        self.zoom_label = QLabel()
        self.compare.zoom_changed.connect(lambda z: self.zoom_label.setText(f"{z:.0%}"))
        mode_bar.addWidget(self.zoom_label)
        compare_tab = QWidget()
        cv = QVBoxLayout(compare_tab)
        cv.addWidget(review)
        cv.addLayout(mode_bar)
        cv.addWidget(self.compare, 1)

        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(5000)
        self.tabs = QTabWidget()
        self.tabs.addTab(compare_tab, "Compare")
        self.tabs.addTab(self.details, "Details")
        self.tabs.addTab(self.log, "Log")

        split = QSplitter()
        split.addWidget(sheets_table)
        split.addWidget(self.tabs)
        split.setSizes([850, 650])
        # One margin around both panes, so their top and bottom edges line up.
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(8, 6, 8, 6)
        cl.addWidget(split)
        self.setCentralWidget(central)

        self.summary = QLabel()
        self.summary.setContentsMargins(8, 0, 0, 0)
        self.statusBar().addWidget(self.summary, 1)

    # ------------------------------------------------------------------ project

    def _mode_changed(self) -> None:
        fixed = self.mode.currentData() == FIXED
        self._order_action.setVisible(not fixed)
        for a in self._fixed_actions:
            a.setVisible(fixed)

    def _new_project(self) -> None:
        name, ok = QInputDialog.getText(self, "New project", f"Project name (stored in {PROJECTS_DIR}):")
        name = name.strip()
        if ok and name:
            if any(c in name for c in '\\/:*?"<>|'):
                QMessageBox.warning(self, "New project", "Use a plain name without \\ / : * ? \" < > |")
                return
            self.open_project(str(project_path(name)))

    def _choose_project(self) -> None:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        d = QFileDialog.getExistingDirectory(self, "Open project folder", str(PROJECTS_DIR))
        if d:
            self.open_project(d)

    def open_project(self, folder: str) -> None:
        if not folder:
            self.summary.setText("Create or open a project (File > New project... / Open project...)")
            return
        self.project = Project(folder)
        self.state = self.project.load_state() or {"sheets": [], "threshold": DEFAULT_THRESHOLD}
        self.thumbnails = Thumbnails(self.project.root / "work" / "thumbs")
        self._set_state(self.state)
        self.settings_store.setValue("project", folder)
        self.setWindowTitle(f"Volumetric Sheet Compressor - {folder}")
        self._update_summary()

    def _set_state(self, state: dict[str, Any]) -> None:
        self.state = state
        self.model.set_state(state, self.thumbnails)
        self.threshold.setValue(state.get("threshold", DEFAULT_THRESHOLD) // KB)

    def _threshold_changed(self, kb: int) -> None:
        """Apply a new threshold at once: table, filter, status bar, saved state and report."""
        value = kb * KB
        if value == self.state.get("threshold") or not self.project:
            return
        self.state["threshold"] = value
        self.model.threshold = value
        self.model.refresh_all()
        self.proxy.set_filter()
        if self.state.get("sheets"):
            self.project.save_state(self.state)
            report.write_reports(self.state, self.project.reports_dir)
        self._update_summary()

    def _open_folder(self, attr: str) -> None:
        if self.project:
            path = getattr(self.project, attr)
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _update_summary(self) -> None:
        sheets = self.state.get("sheets", [])
        if not sheets:
            self.summary.setText("No scan yet: use Scan...")
            return
        s = report.scan_summary(self.state)
        t = report.compress_totals(x for x in sheets if x.get("compression"))
        approved = sum(1 for x in sheets if pipeline.is_approved(x))
        unexported = any(pipeline.is_approved(x) != bool(x.get("output")) for x in sheets)
        self.summary.setText(
            f"{s['sheets']} sheets used {s['references']} times | {s['over_threshold']} over "
            f"{format_size(s['threshold'])} ({format_size(s['over_threshold_bytes'])}) | "
            f"compressed {t['counts'].get('compressed', 0)}: {format_size(t['before'])} -> {format_size(t['after'])} | "
            f"{approved} approved{' - not exported yet' if unexported else ''} | {len(self.model.checked)} checked")

    # ------------------------------------------------------------------ selection / display

    def _selected_keys(self) -> list[str]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.sheet(r)["key"] for r in sorted(rows)]

    def _current_sheet(self) -> dict[str, Any] | None:
        idx = self.table.selectionModel().currentIndex()
        return self.model.sheet(self.proxy.mapToSource(idx).row()) if idx.isValid() else None

    def _set_checked(self, keys: set[str]) -> None:
        self.model.set_checked(keys)
        self._update_summary()

    def _check_over_threshold(self) -> None:
        over = {s["key"] for s in self.state.get("sheets", []) if report.over_threshold(s, self.model.threshold)}
        self._set_checked(self.model.checked | over)

    def _on_table_clicked(self, index) -> None:
        if url := self.model.link(self.proxy.mapToSource(index).row(), index.column()):
            QDesktopServices.openUrl(QUrl(url))

    def _show_current(self) -> None:
        s = self._current_sheet()
        if s is None:
            self.compare.set_images(None, None, ("", ""), "Select a sheet")
            self.details.setHtml("")
            return
        comp = s.get("compression") or {}
        ch, src = comp.get("chosen") or {}, comp.get("source") or {}
        original = (s.get("download") or {}).get("path")
        orig_label = f"Original {src.get('format', '')} {format_size(src.get('size') or report.size_of(s))}"
        cand_label = f"{candidate_name(ch['format'], ch['quality'])} {format_size(ch['size'])}" if ch else "No result"
        if not original:
            message = "Not downloaded yet - tick it and use Download checked to preview"
        elif comp:
            score = "" if ch.get("score") is None else f"worst tile {ch['score']}  -  "
            message = f"{score}{comp['status']}  {comp.get('message') or ''}"
        else:
            message = ""
        self.compare.set_images(original, comp.get("candidate_path"), (orig_label, cand_label), message)
        self.details.setHtml(details_html(s))

    # ------------------------------------------------------------------ background jobs

    def _run(self, title: str, fn: Callable[[worker.Job], Any], done: Callable[[Any], None] | None = None,
             can_stop: bool = True) -> None:
        if self.job is not None:
            return
        self.progress.start(title, can_stop)
        job = self.job = worker.start(fn)
        job.log.connect(self.log.appendPlainText)
        job.log.connect(self.progress.set_detail)
        job.progress.connect(self.progress.set_progress)
        job.sheet_done.connect(self._on_sheet_done)
        job.finished.connect(lambda result: self._job_ended(done, result))
        job.failed.connect(self._job_failed)

    def _on_sheet_done(self, key: str) -> None:
        self.model.refresh(key)
        self._update_summary()

    def _job_ended(self, done: Callable[[Any], None] | None, result: Any) -> None:
        self.job = None
        self.progress.finish()
        if done:
            done(result)
        self._update_summary()
        self._show_current()

    def _job_failed(self, tb: str) -> None:
        self.log.appendPlainText(tb)
        self.job = None
        self.progress.finish()
        QMessageBox.critical(self, "Error", tb.strip().splitlines()[-1])

    def stop(self) -> None:
        if self.job:
            self.job.stop()
            self.log.appendPlainText("Stopping: finishing what's already running...")

    def _settings(self) -> Settings:
        fixed_format = self.fixed_format.currentData()
        return Settings(
            target=float(self.target.value()),
            mode=self.mode.currentData(),
            formats=ORDERS[self.order.currentText()],
            fixed_format=fixed_format,
            fixed_quality=None if fixed_format == "jpeg-keep" else self.fixed_quality.value(),
        )

    # ------------------------------------------------------------------ commands

    def _require_project(self) -> bool:
        if self.project is None:
            self._choose_project()
        return self.project is not None

    def scan(self) -> None:
        if not self._require_project():
            return
        dlg = ScanDialog(self, self.state)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v, project, threshold = dlg.values(), self.project, self.state.get("threshold", DEFAULT_THRESHOLD)

        def work(job: worker.Job):
            state, _ = pipeline.scan_project(
                project, v["paths"], threshold, v["exclude"], refresh=v["refresh"],
                file_progress=job.progress_for("Reading files"), url_progress=job.progress_for("Checking URLs"),
                log=job.log.emit, should_stop=job.stopped)
            return state

        self._run("Scanning", work, self._set_state)

    def _checked_sheets(self, verb: str) -> list[dict[str, Any]]:
        """Ticked sheets in the order the table shows them (a hint if there are none)."""
        order = {k: i for i, k in enumerate(self.proxy.visible_keys())}
        sheets = sorted((s for s in self.state.get("sheets", []) if s["key"] in self.model.checked),
                        key=lambda s: order.get(s["key"], len(order)))
        if not sheets:
            QMessageBox.information(self, verb, f"Tick the sheets to {verb.lower()} first.")
        return sheets

    def download(self) -> None:
        if not (self.project and (sheets := self._checked_sheets("Download"))):
            return
        project, state = self.project, self.state

        def work(job: worker.Job):
            pipeline.download(project, state, sheets, progress=job.progress_for("Downloading"), log=job.log.emit,
                              on_sheet_done=job.sheet_done.emit, should_stop=job.stopped)

        self._run(f"Downloading {len(sheets)} sheets", work)

    def compress(self) -> None:
        if not (self.project and (sheets := self._checked_sheets("Compress"))):
            return
        project, state, settings, auto = self.project, self.state, self._settings(), self.auto_approve.isChecked()
        self.log.appendPlainText(f"Compressing {len(sheets)} sheets: {settings.describe()}")

        def work(job: worker.Job):
            # The user picked these sheets, so no threshold applies (threshold 0).
            pipeline.run(project, state, sheets, settings, 0, auto_approve=auto, log=job.log.emit,
                         download_progress=job.progress_for("Downloading"), progress=job.progress_for("Sheets done"),
                         on_sheet_done=job.sheet_done.emit, should_stop=job.stopped)

        self._run(f"Compressing {len(sheets)} sheets", work)

    def reencode(self) -> None:
        s = self._current_sheet()
        if not (self.project and s):
            return
        fmt = self.re_format.currentData()
        quality = None if fmt == "jpeg-keep" else self.re_quality.value()
        project, state, settings, key = self.project, self.state, self._settings(), s["key"]

        def work(job: worker.Job):
            res = pipeline.reencode(project, state, key, settings, fmt, quality, log=job.log.emit)
            job.log.emit(f"=> {res.describe()}")
            job.sheet_done.emit(key)

        self._run(f"Re-encoding as {candidate_name(fmt, quality)}", work, can_stop=False)

    def export(self) -> None:
        if not self.project:
            return
        project, state = self.project, self.state

        def work(job: worker.Job):
            return pipeline.export(project, state, progress=job.progress_for("Sheets"), log=job.log.emit)

        def done(counts):
            self.model.refresh_all()
            QMessageBox.information(self, "Export", f"{counts['in_out']} files in out/ "
                                    f"({counts['copied']} copied, {counts['removed']} removed). Reports updated.")

        self._run("Exporting", work, done, can_stop=False)

    def map_uploads(self) -> None:
        if not self.project:
            return
        dlg = MapDialog(self, self.settings_store.value("cloudinfo") or "")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.path:
            self.settings_store.setValue("cloudinfo", dlg.path)
        self.log.appendPlainText(f"Reading uploads from {dlg.path or 'pasted URLs'}")
        self._map([dlg.data])

    def _map(self, sources: list[bytes]) -> None:
        project, state = self.project, self.state

        def work(job: worker.Job):
            return mapping.map_uploads(project, state, sources, log=job.log.emit,
                                       progress=job.progress_for("Checking new URLs"))

        def done(c):
            self.model.refresh_all()
            QMessageBox.information(self, "Map uploads",
                                    f"Found new URLs for {c['mapped']} of {c['exported']} exported sheets.\n"
                                    f"Not uploaded yet: {c['not_uploaded']}.  New URLs that failed the check: "
                                    f"{c['broken']}.")

        self._run("Mapping uploads", work, done, can_stop=False)

    def apply(self) -> None:
        if not self.project:
            return
        project, state = self.project, self.state

        def preview(job: worker.Job):
            return mapping.apply(project, state, dry_run=True, log=job.log.emit, progress=job.progress_for("Files"))

        def confirm(result):
            if not result["replacements"]:
                QMessageBox.information(self, "Apply", "Nothing to apply: no approved sheet has a working new URL "
                                        "(or the files already use the new URLs).")
                return
            files = [f["path"] for f in result["files"]]
            listing = "\n".join(files[:12]) + (f"\n... and {len(files) - 12} more" if len(files) > 12 else "")
            if QMessageBox.question(
                    self, "Apply",
                    f"Replace {result['replacements']} URLs for {len(result['sheets'])} sheets in {len(files)} files?"
                    f"\n\nBackups are kept and 'Undo last apply' restores them.\n\n{listing}"
            ) != QMessageBox.StandardButton.Yes:
                return
            self._run("Applying new URLs", lambda job: mapping.apply(
                project, state, log=job.log.emit, progress=job.progress_for("Files")), applied, can_stop=False)

        def applied(result):
            self.model.refresh_all()
            errors = f"\n\n{len(result['errors'])} files failed (see Log)." if result["errors"] else ""
            for e in result["errors"]:
                self.log.appendPlainText(f"! {e}")
            QMessageBox.information(self, "Apply", f"Replaced {result['replacements']} URLs in "
                                    f"{len(result['files'])} files.\nBackups: {result['backup']}{errors}")

        self._run("Previewing changes", preview, confirm, can_stop=False)

    def undo_apply(self) -> None:
        if not self.project or not self.state.get("history"):
            QMessageBox.information(self, "Undo", "Nothing to undo.")
            return
        last = self.state["history"][-1]
        if QMessageBox.question(self, "Undo", f"Restore the {len(last['files'])} files changed on {last['at']}?") \
                != QMessageBox.StandardButton.Yes:
            return
        project, state = self.project, self.state
        self._run("Restoring files", lambda job: mapping.undo(project, state, log=job.log.emit),
                  lambda n: self.model.refresh_all(), can_stop=False)

    def _set_approval(self, approval: str) -> None:
        if not self.project or self.job:
            return
        keys = self._selected_keys()
        skipped = pipeline.set_approval(self.project, self.state, keys, approval)
        for key in keys:
            self.model.refresh(key)
        self._update_summary()
        self._show_current()
        if skipped:
            QMessageBox.information(self, "Approve", f"{len(skipped)} of the selected sheets have no compressed "
                                    "result yet, so they can't be approved.")

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.job and QMessageBox.question(self, "Quit", "A job is running. Stop it and quit?") \
                != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        if self.job:
            self.job.stop()
        e.accept()


def make_app(argv: list[str]) -> QApplication:
    app = QApplication(argv)
    app.setOrganizationName("VolumetricSheetCompressor")
    app.setApplicationName("vsc")
    app.setApplicationDisplayName("Volumetric Sheet Compressor")
    return app


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    app = make_app(argv)
    win = MainWindow(argv[1] if len(argv) > 1 else None)
    win.show()
    return app.exec()
