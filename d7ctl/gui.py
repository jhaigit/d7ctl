"""d7ctl-gui - graphical control of a Kenwood TH-D7 / TH-D7G.

Tabs: Radio (both bands, PTT, global switches), Memories (all 222
channels, edit in place, CSV import/export), Settings (every menu
setting), Console (serial traffic and raw commands).

All radio I/O runs on one worker thread, in order, so the window never
blocks; auto-info reports from the radio arrive through Qt signals.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import threading
import time

from PySide6.QtCore import (QObject, QRegularExpression, QRunnable, QSettings, Qt, QThreadPool,
                            QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QFontMetrics, QPainter,
                           QPalette, QPen,
                           QRegularExpressionValidator)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSlider, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionComboBox,
    QStyleOptionViewItem, QStylePainter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import memfile
from . import protocol as P
from .radio import CommandRejected, D7Radio, RadioError
from .settings import GROUPS, settings_for

TX_TIMEOUT_MS = 180_000     # PTT released automatically after 3 min (stuck-key guard)
POLL_MS = 1000              # meter polling interval when auto-info is off
CONSOLE_LINES = 5000        # console scroll-back

DIRTY = QColor(255, 236, 160)
INVALID = QColor(255, 190, 190)


# ---------------------------------------------------------------------------
# worker: every radio call goes through one thread, in order
# ---------------------------------------------------------------------------

class _JobSignals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(object)

    def __init__(self, on_done, on_error, on_progress, finished):
        super().__init__()
        self._cbs = (on_done, on_error, on_progress)
        self._finished = finished
        self.done.connect(self._on_done)
        self.failed.connect(self._on_failed)
        self.progress.connect(self._on_progress)

    def _on_done(self, value):
        try:
            if self._cbs[0]:
                self._cbs[0](value)
        finally:
            self._finished(self)

    def _on_failed(self, exc):
        try:
            if self._cbs[1]:
                self._cbs[1](exc)
        finally:
            self._finished(self)

    def _on_progress(self, value):
        if self._cbs[2]:
            self._cbs[2](value)


class _Job(QRunnable):
    def __init__(self, fn, signals, with_progress):
        super().__init__()
        self.fn, self.signals, self.with_progress = fn, signals, with_progress
        self.setAutoDelete(True)

    def run(self):
        try:
            if self.with_progress:
                value = self.fn(self.signals.progress.emit)
            else:
                value = self.fn()
        except Exception as exc:          # delivered to the GUI thread
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(value)


class Worker(QObject):
    def __init__(self, on_error):
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)    # strict FIFO: commands stay in order
        self._live = set()
        self._default_error = on_error

    @property
    def pending(self) -> int:
        return len(self._live)

    def submit(self, fn, on_done=None, on_error=None, on_progress=None):
        sig = _JobSignals(on_done, on_error or self._default_error, on_progress, self._live.discard)
        self._live.add(sig)
        self.pool.start(_Job(fn, sig, on_progress is not None))


class Bridge(QObject):
    """Carries radio-thread callbacks into the GUI thread."""
    event = Signal(str)
    traffic = Signal(str, str)


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------

def mono_font(size: int, bold: bool = False) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(size)
    f.setBold(bold)
    return f


def limit_to_memory_name(edit: QLineEdit) -> QLineEdit:
    """Stop typing at what MNA can store: NAME_LEN characters, no comma (the field separator)."""
    edit.setMaxLength(P.NAME_LEN)
    edit.setValidator(QRegularExpressionValidator(QRegularExpression(f"[^,]{{0,{P.NAME_LEN}}}"), edit))
    edit.setPlaceholderText(f"up to {P.NAME_LEN} characters")
    return edit


MEMORY_LIST_ROWS = 12        # drop-down height of the memory lists
MEMORY_LABEL_CHARS = 4 + P.NAME_LEN   # "000 " and a full name


UNREAD_MARK = "?"             # after the id of a memory not read yet, drawn in UNREAD_COLOUR
UNREAD_COLOUR = QColor("#d08000")
UNREAD_ROLE = Qt.UserRole + 1 # item flag: memory not read yet (a name may itself end in "?")


def memory_label(memid: str, data: dict) -> str:
    """'000 W1AW RPT' (the name), '005 146.520' (no name: the frequency), '004'
    (empty) or '030 ?' (not read yet). Short: the band panels are narrow."""
    if memid not in data:
        return f"{memid} {UNREAD_MARK}"
    ch = data[memid]
    if ch is None:
        return memid
    return f"{memid} {ch.name if ch.name.strip() else P.format_mhz(ch.freq, 3)}"


def _draw_label_with_mark(painter, font, rect, text: str, colour: QColor) -> None:
    """Draw 'id ?' at rect's left, vertically centred: the id in colour, the mark in UNREAD_COLOUR."""
    before = text[:-len(UNREAD_MARK)]
    fm = QFontMetrics(font)
    painter.save()
    painter.setFont(font)
    painter.setPen(colour)
    painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, before)
    painter.setPen(UNREAD_COLOUR)
    painter.drawText(rect.adjusted(fm.horizontalAdvance(before), 0, 0, 0),
                     Qt.AlignLeft | Qt.AlignVCenter, UNREAD_MARK)
    painter.restore()


class MemoryItemDelegate(QStyledItemDelegate):
    """Memory list rows: the '?' of an unread memory in colour."""

    def paint(self, painter, option, index):
        if not index.data(UNREAD_ROLE):
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""             # the style draws the row, we draw the text
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget)
        # the inset QCommonStyle gives item text, so the row lines up with the others
        rect.adjust(style.pixelMetric(QStyle.PM_FocusFrameHMargin, None, widget) + 1, 0, 0, 0)
        selected = opt.state & QStyle.State_Selected
        colour = opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text)
        _draw_label_with_mark(painter, opt.font, rect, text, colour)


class MemoryCombo(QComboBox):
    """The closed box shows the '?' of an unread memory in colour too."""

    def paintEvent(self, ev):
        if not self.currentData(UNREAD_ROLE):
            return super().paintEvent(ev)
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        text, opt.currentText = opt.currentText, ""
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        rect = self.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, self)
        rect.adjust(1, 0, -1, 0)                    # as QCommonStyle's CE_ComboBoxLabel
        _draw_label_with_mark(p, self.font(), rect, text, self.palette().color(QPalette.ButtonText))


def memory_combo() -> QComboBox:
    """A memory list: item data = memory id; short and scrolling, in columns."""
    c = MemoryCombo()
    for memid in P.memory_ids():
        if not P.is_call(memid):
            c.addItem(memory_label(memid, {}), memid)
            c.setItemData(c.count() - 1, True, UNREAD_ROLE)
    c.setItemDelegate(MemoryItemDelegate(c))
    c.setMaxVisibleItems(MEMORY_LIST_ROWS)
    # some styles (Fusion, GTK) open a combo as a screen-high popup and ignore
    # maxVisibleItems; this style property makes them use a short scrolling list
    c.setStyleSheet("QComboBox { combobox-popup: 0; }")
    c.setFont(mono_font(c.font().pointSize()))           # columns line up
    # never narrower than a full label: 3-character id, a space, an 8-character name
    c.setMinimumContentsLength(MEMORY_LABEL_CHARS)
    c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    c.view().setMinimumWidth(c.fontMetrics().horizontalAdvance("W" * MEMORY_LABEL_CHARS)
                             + c.style().pixelMetric(QStyle.PM_ScrollBarExtent) + 12)
    return c


def relabel_memory_combo(c: QComboBox, data: dict, only=None) -> None:
    for i in range(c.count()):
        memid = c.itemData(i)
        if only is None or memid == only:
            c.setItemText(i, memory_label(memid, data))
            c.setItemData(i, memid not in data, UNREAD_ROLE)


def tone_items():
    return [f"{t:.1f}" for t in P.valid_tones()]


class BandPanel(QGroupBox):
    """Everything about one band."""

    def __init__(self, band: int, main: "MainWindow"):
        super().__init__(f"Band {P.BANDS[band]}")
        self.band, self.main = band, main
        self.ch: P.Channel | None = None
        self.band_mode = 0

        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.ctrl = QPushButton("CTRL")
        self.ctrl.setCheckable(True)
        self.ctrl.setToolTip("Make this the control band (PTT and keypad act on it)")
        self.ctrl.clicked.connect(lambda: main.run(lambda r: r.set_control_band(band), main.refresh_globals))
        top.addWidget(self.ctrl)
        self.mode_group = QButtonGroup(self)
        for m, label in P.BAND_MODES.items():
            b = QPushButton(label)
            b.setCheckable(True)
            self.mode_group.addButton(b, m)
            top.addWidget(b)
        self.mode_group.idClicked.connect(self._set_mode)
        top.addStretch(1)
        self.busy = QLabel("BUSY")
        self.busy.setAlignment(Qt.AlignCenter)
        self.busy.setMinimumWidth(56)
        top.addWidget(self.busy)
        lay.addLayout(top)

        self.display = QLabel("---.-----")
        self.display.setFont(mono_font(28, bold=True))
        self.display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.display.setStyleSheet("QLabel { background: #17241a; color: #b9f6a6; "
                                   "border-radius: 6px; padding: 6px 12px; }")
        lay.addWidget(self.display)
        self.subline = QLabel(" ")
        self.subline.setFont(mono_font(10))
        lay.addWidget(self.subline)

        meter = QHBoxLayout()
        meter.addWidget(QLabel("S"))
        self.smeter = QProgressBar()
        self.smeter.setRange(0, P.SMETER_MAX)
        self.smeter.setFormat("%v")
        meter.addWidget(self.smeter, 1)
        lay.addLayout(meter)

        tune = QHBoxLayout()
        self.freq_edit = QLineEdit()
        self.freq_edit.setPlaceholderText("MHz, e.g. 145.525")
        self.freq_edit.returnPressed.connect(self._set_freq)
        tune.addWidget(self.freq_edit, 1)
        go = QPushButton("Set")
        go.clicked.connect(self._set_freq)
        tune.addWidget(go)
        for text, d in (("▼", -1), ("▲", +1)):
            b = QPushButton(text)
            b.setFixedWidth(36)
            b.setToolTip("One step (VFO) or channel (MR) " + ("up" if d > 0 else "down"))
            b.clicked.connect(lambda _=False, d=d: self.main.run(
                lambda r: r.step(band, d), lambda _: self.main.refresh_band(band)))
            tune.addWidget(b)
        lay.addLayout(tune)

        mem = QHBoxLayout()
        mem.addWidget(QLabel("Memory"))
        self.mem_combo = memory_combo()
        self.mem_combo.setToolTip("Names show once a memory has been read (Memories tab, or Store VFO)")
        mem.addWidget(self.mem_combo, 1)
        recall = QPushButton("Recall")
        recall.clicked.connect(self._recall)
        mem.addWidget(recall)
        store = QPushButton("Store VFO…")
        store.setToolTip("Save this band's current frequency and settings in a memory")
        store.clicked.connect(self._store)
        mem.addWidget(store)
        to_vfo = QPushButton("M→V")
        to_vfo.setToolTip("Copy the current memory into the VFO (MSH)")
        to_vfo.clicked.connect(lambda: self.main.run(lambda r: r.memory_to_vfo(band),
                                                     lambda _: self.main.refresh_band(band)))
        mem.addWidget(to_vfo)
        lay.addLayout(mem)

        form = QFormLayout()
        self.step = QComboBox()
        self.step.addItems([f"{s:g} kHz" for s in P.STEPS_KHZ])
        self.step.activated.connect(lambda i: self._edit(step=i))
        form.addRow("Step", self.step)

        sh = QHBoxLayout()
        self.shift = QComboBox()
        self.shift.addItems(list(P.SHIFTS.values()))
        self.shift.activated.connect(lambda i: self._edit(shift=i))
        sh.addWidget(self.shift)
        self.offset = QDoubleSpinBox()
        self.offset.setDecimals(3)
        self.offset.setRange(0, 99.995)
        self.offset.setSingleStep(0.05)
        self.offset.setSuffix(" MHz")
        self.offset.editingFinished.connect(self._offset_done)
        sh.addWidget(self.offset)
        self.rev = QCheckBox("Reverse")
        self.rev.clicked.connect(lambda on: self._edit(reverse=on))
        sh.addWidget(self.rev)
        form.addRow("Shift", sh)

        tn = QHBoxLayout()
        self.tone_mode = QComboBox()
        self.tone_mode.addItems(["Off", "Tone", "CTCSS"])
        self.tone_mode.activated.connect(lambda i: self._edit(tone=i == 1, ctcss=i == 2))
        tn.addWidget(self.tone_mode)
        tn.addWidget(QLabel("T"))
        self.tone = QComboBox()
        self.tone.addItems(tone_items())
        self.tone.activated.connect(lambda i: self._edit(tone_code=P.tone_code(float(self.tone.itemText(i)))))
        tn.addWidget(self.tone)
        tn.addWidget(QLabel("CT"))
        self.ctcss = QComboBox()
        self.ctcss.addItems(tone_items())
        self.ctcss.activated.connect(lambda i: self._edit(ctcss_code=P.tone_code(float(self.ctcss.itemText(i)))))
        tn.addWidget(self.ctcss)
        form.addRow("Tone", tn)

        mp = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(list(P.MODES.values()))
        self.mode.activated.connect(lambda i: self._edit(mode=i))
        mp.addWidget(self.mode)
        mp.addWidget(QLabel("Power"))
        self.power = QComboBox()
        for code, label in P.POWERS.items():
            self.power.addItem(label, code)
        self.power.activated.connect(
            lambda i: self.main.run(lambda r: r.set_power(band, self.power.itemData(i))))
        mp.addWidget(self.power)
        form.addRow("Mode", mp)

        sq = QHBoxLayout()
        self.squelch = QSlider(Qt.Horizontal)
        self.squelch.setRange(0, P.SQUELCH_MAX)
        self.squelch.setPageStep(1)
        self.squelch.setTickPosition(QSlider.TicksBelow)
        self.squelch_label = QLabel("-")
        self.squelch.valueChanged.connect(lambda v: self.squelch_label.setText(str(v)))
        self.squelch.sliderReleased.connect(self._squelch_done)
        self.squelch.actionTriggered.connect(lambda _a: QTimer.singleShot(0, self._squelch_done))
        sq.addWidget(self.squelch, 1)
        sq.addWidget(self.squelch_label)
        form.addRow("Squelch", sq)
        lay.addLayout(form)
        self._sq_sent = None
        self.set_busy(False)

    # -- actions ---------------------------------------------------------
    def _edit(self, **changes):
        b = self.band
        self.main.run(lambda r: r.update_vfo(b, **changes), self.show_channel)

    def _offset_done(self):
        hz = int(round(self.offset.value() * 1e6))
        if self.ch is not None and hz != self.ch.offset:
            self._edit(offset=hz)

    def _squelch_done(self):
        if self.squelch.isSliderDown():
            return              # wait for the release, not every pixel of the drag
        v = self.squelch.value()
        if v != self._sq_sent:
            self._sq_sent = v
            b = self.band
            self.main.run(lambda r: r.set_squelch(b, v))

    def _set_freq(self):
        text = self.freq_edit.text().strip()
        if not text:
            return
        try:
            hz = P.parse_frequency(text)
        except ValueError:
            self.main.error(f"not a frequency: {text}")
            return
        b = self.band
        self.main.run(lambda r: r.set_frequency(b, hz), self.show_channel)
        self.freq_edit.clear()

    def _set_mode(self, m):
        b = self.band
        self.main.run(lambda r: r.set_band_mode(b, m), lambda _: self.main.refresh_band(b))

    def _recall(self):
        memid = self.mem_combo.currentData()
        b = self.band
        self.main.run(lambda r: r.recall_memory(b, memid), lambda _: self.main.refresh_band(b))

    def _store(self):
        dlg = StoreDialog(self.main, self.band, self)
        if not dlg.exec():
            return
        memid, name = dlg.memid, dlg.name.text()
        b = self.band
        self.main.run(lambda r: r.vfo_to_memory(b, memid, name),
                      lambda ch: self.main.memories.update_row(memid, ch))

    # -- display -----------------------------------------------------------
    def show_status(self, st: dict):
        if st.get("hidden"):
            self.setEnabled(False)
            self.display.setText("— hidden —")
            self.subline.setText("single-band display: turn Dual on to use this band")
            return
        self.setEnabled(True)
        self.band_mode = st["mode"]
        b = self.mode_group.button(st["mode"])
        if b:
            b.setChecked(True)
        if st.get("memory"):
            i = self.mem_combo.findData(st["memory"])
            if i >= 0:
                self.mem_combo.setCurrentIndex(i)
        if st.get("power") is not None:
            self.power.setCurrentIndex(self.power.findData(st["power"]))
        if st.get("squelch") is not None:
            self._sq_sent = st["squelch"]
            self.squelch.blockSignals(True)
            self.squelch.setValue(st["squelch"])
            self.squelch.blockSignals(False)
            self.squelch_label.setText(str(st["squelch"]))
        if st.get("smeter") is not None:
            self.smeter.setValue(st["smeter"])
        if st.get("busy") is not None:
            self.set_busy(st["busy"])
        self.mem_name = st.get("name", "")
        self.show_channel(st["vfo"], st.get("memory"))

    def show_channel(self, ch: P.Channel, memid=None):
        self.ch = ch
        self.display.setText(f"{ch.freq / 1e6:.5f}")
        mode = P.BAND_MODES.get(self.band_mode, "")
        where = f"{mode} {self.mem_combo.currentData()}" if self.band_mode == 2 else mode
        if self.band_mode == 2 and getattr(self, "mem_name", ""):
            where += f" {self.mem_name}"
        extra = []
        if ch.shift:
            extra.append(f"{P.SHIFT_SYMBOLS[ch.shift]}{ch.offset / 1e6:.3f}")
        if ch.tone:
            extra.append(f"T {ch.tone_hz}")
        if ch.ctcss:
            extra.append(f"CT {ch.ctcss_hz}")
        if ch.reverse:
            extra.append("REV")
        self.subline.setText(f"{where:8} {P.MODES[ch.mode]}  {'  '.join(extra)}")
        self.step.setCurrentIndex(ch.step)
        self.shift.setCurrentIndex(ch.shift if ch.shift < self.shift.count() else 0)
        self.offset.blockSignals(True)
        self.offset.setValue(ch.offset / 1e6)
        self.offset.blockSignals(False)
        self.rev.setChecked(ch.reverse)
        self.tone_mode.setCurrentIndex(2 if ch.ctcss else 1 if ch.tone else 0)
        for combo, hz in ((self.tone, ch.tone_hz), (self.ctcss, ch.ctcss_hz)):
            if hz is not None:
                combo.setCurrentIndex(combo.findText(f"{hz:.1f}"))
        self.mode.setCurrentIndex(ch.mode)

    def set_busy(self, busy: bool):
        self.busy.setStyleSheet(
            "QLabel { border-radius: 4px; padding: 2px 6px; font-weight: bold; "
            + ("background: #2e9d4a; color: white; }" if busy else "background: #d8d8d8; color: #888; }"))

    def set_control(self, is_ctrl: bool):
        self.ctrl.setChecked(is_ctrl)
        self.ctrl.setStyleSheet("QPushButton:checked { background: #e0a020; font-weight: bold; }")


class StoreDialog(QDialog):
    """Pick the memory to store a band's VFO in, seeing which are in use.

    What a memory holds comes from the Memories tab when it has been read;
    otherwise the dialog reads just that memory (one MR) when it is picked.
    """

    def __init__(self, main: "MainWindow", band: int, parent=None):
        super().__init__(parent)
        self.main, self.band = main, band
        self.ids = [m for m in P.memory_ids() if not P.is_call(m)]
        self.searching = False
        self.setWindowTitle(f"Store band {P.BANDS[band]} in a memory")
        form = QFormLayout(self)
        row = QHBoxLayout()
        self.combo = memory_combo()
        relabel_memory_combo(self.combo, self.main.memories.data)
        self.combo.currentIndexChanged.connect(self._picked)
        row.addWidget(self.combo, 1)
        self.next_btn = QPushButton("Next empty")
        self.next_btn.setToolTip("Find the next empty memory after this one (reads the radio as needed)")
        self.next_btn.clicked.connect(self.next_empty)
        row.addWidget(self.next_btn)
        form.addRow("Memory", row)
        self.state = QLabel()
        form.addRow("", self.state)
        self.name = limit_to_memory_name(QLineEdit())
        form.addRow("Name", self.name)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        known_empty = [i for i, m in enumerate(self.ids) if self._known(m) and self._held(m) is None]
        self.combo.setCurrentIndex(known_empty[0] if known_empty else 0)
        self._picked(self.combo.currentIndex())

    @property
    def memid(self) -> str:
        return self.ids[self.combo.currentIndex()]

    def _known(self, memid) -> bool:
        return memid in self.main.memories.data

    def _held(self, memid):
        return self.main.memories.data.get(memid)

    def _show_state(self):
        memid = self.memid
        ok = self.buttons.button(QDialogButtonBox.Ok)
        if not self._known(memid):
            self.state.setText("checking…")
            ok.setEnabled(False)
            return
        ok.setEnabled(not self.searching)
        ch = self._held(memid)
        if ch is None:
            self.state.setText("empty")
            self.state.setStyleSheet("")
            ok.setText("Store")
        else:
            self.state.setText(f"in use: {ch.name or '(no name)'}  {ch.summary()} - storing replaces it")
            self.state.setStyleSheet("color: #c06000")
            ok.setText("Overwrite")

    def _picked(self, i):
        memid = self.ids[i]
        self._show_state()
        if not self._known(memid):
            self._read(memid, lambda: None)

    def _read(self, memid, then):
        def done(ch):
            self.main.memories.update_row(memid, ch)        # the Memories tab learns it too
            relabel_memory_combo(self.combo, self.main.memories.data, memid)
            if memid == self.memid:
                self._show_state()
            then()

        def failed(exc):
            self._stop_search(f"could not read {memid}: {exc}")
        self.main.run(lambda r: r.read_memory(memid), done, failed)

    def next_empty(self):
        self.searching = True
        self.next_btn.setEnabled(False)
        self._search(self.combo.currentIndex() + 1)

    def _search(self, i):
        while i < len(self.ids) and self._known(self.ids[i]) and self._held(self.ids[i]) is not None:
            i += 1
        if i >= len(self.ids):
            self._stop_search("no empty memory after this one")
        elif self._known(self.ids[i]):
            self._stop_search()
            self.combo.setCurrentIndex(i)
        else:
            self.state.setText(f"checking {self.ids[i]}…")
            self._read(self.ids[i], lambda: self._search(i))

    def _stop_search(self, message=None):
        self.searching = False
        self.next_btn.setEnabled(True)
        self._show_state()
        if message:
            self.state.setText(message)


class RadioTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        bands = QHBoxLayout()
        self.panels = [BandPanel(0, main), BandPanel(1, main)]
        for p in self.panels:
            bands.addWidget(p)
        lay.addLayout(bands)

        row = QHBoxLayout()
        self.ptt = QPushButton("PTT  (hold)")
        self.ptt.setMinimumHeight(48)
        self.ptt.setStyleSheet("QPushButton { background: #b03030; color: white; font-weight: bold; "
                               "font-size: 15px; border-radius: 6px; } QPushButton:pressed { background: #ff3b3b; }")
        self.ptt.setToolTip("Transmits on the control band while held "
                            f"(released automatically after {TX_TIMEOUT_MS // 1000} s)")
        self.ptt.pressed.connect(lambda: main.key_tx(True, "TX"))
        self.ptt.released.connect(lambda: main.key_tx(False))
        row.addWidget(self.ptt, 2)
        self.t1750 = QPushButton("1750 Hz (hold)")
        self.t1750.setMinimumHeight(48)
        self.t1750.pressed.connect(lambda: main.key_tx(True, "TT"))
        self.t1750.released.connect(lambda: main.key_tx(False))
        row.addWidget(self.t1750, 1)
        lay.addLayout(row)

        g = QGroupBox("Radio")
        gl = QHBoxLayout(g)
        self.toggles = {}
        for key, label in (("DL", "Dual band"), ("LK", "Lock"), ("LMP", "Lamp"),
                           ("MON", "Monitor"), ("TXS", "TX inhibit")):
            cb = QCheckBox(label)
            cb.clicked.connect(lambda on, k=key: main.set_setting_value(k, on, main.refresh_all))
            gl.addWidget(cb)
            self.toggles[key] = cb
        gl.addWidget(QLabel("Balance A:B"))
        self.balance = QComboBox()
        self.balance.addItems(["4:0", "3:1", "2:2", "1:3", "0:4"])
        self.balance.activated.connect(lambda i: main.set_setting_value("BAL", i))
        gl.addWidget(self.balance)
        self.scan = QPushButton("Scan")
        self.scan.setCheckable(True)
        self.scan.clicked.connect(lambda on: main.run(lambda r: r.scan(on)))
        gl.addWidget(self.scan)
        gl.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(main.refresh_all)
        gl.addWidget(refresh)
        lay.addWidget(g)
        lay.addStretch(1)


# ---------------------------------------------------------------------------
# band scan
# ---------------------------------------------------------------------------

S_MAX = 5                    # the D7's S-meter reads 0-5
SWEEP_MAX_POINTS = 4000      # a 5 kHz sweep of 20 MHz; beyond this a sweep takes too long
SWEEP_DWELL_MS = 100         # default receiver settling time per point (not yet measured)


class SpectrumView(QWidget):
    """S-meter against frequency as bars; click a bar to tune there."""
    clicked = Signal(int)                 # Hz
    hovered = Signal(int, object)         # Hz, level or None

    MARGIN_L, MARGIN_B, MARGIN_T = 26, 18, 6

    def __init__(self):
        super().__init__()
        self.freqs: list = []
        self.levels: list = []            # this sweep, None = not read or refused
        self.peaks: list = []             # highest level seen at each point
        self.cursor = -1                  # point being measured
        self.marker: int | None = None   # Hz the band was tuned to by a click
        self.setMouseTracking(True)
        self.setMinimumHeight(160)
        self.setCursor(Qt.CrossCursor)

    def set_frequencies(self, freqs):
        if freqs != self.freqs:
            self.freqs = list(freqs)
            self.peaks = [None] * len(freqs)
        self.levels = [None] * len(freqs)
        self.cursor = -1
        self.update()

    def clear_peaks(self):
        self.peaks = [None] * len(self.freqs)
        self.update()

    def set_level(self, i, level):
        self.levels[i] = level
        if level is not None and (self.peaks[i] is None or level > self.peaks[i]):
            self.peaks[i] = level
        self.cursor = i
        self.update()

    def _plot_rect(self):
        return (self.MARGIN_L, self.MARGIN_T, max(1, self.width() - self.MARGIN_L - 4),
                max(1, self.height() - self.MARGIN_T - self.MARGIN_B))

    def index_at(self, x) -> int:
        left, _, w, _ = self._plot_rect()
        if not self.freqs:
            return -1
        i = int((x - left) * len(self.freqs) / w)
        return min(max(i, 0), len(self.freqs) - 1)

    def paintEvent(self, ev):
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QPalette.Base))
        left, top, w, h = self._plot_rect()
        text = pal.color(QPalette.Text)
        grid = pal.color(QPalette.Mid)
        p.setFont(mono_font(8))
        for s in range(S_MAX + 1):                       # S grid and labels
            y = top + h - h * s / S_MAX
            p.setPen(QPen(grid, 1, Qt.DotLine))
            p.drawLine(left, int(y), left + w, int(y))
            p.setPen(text)
            p.drawText(2, int(y) + 4, f"S{s}")
        n = len(self.freqs)
        if n:
            bw = w / n
            bar = pal.color(QPalette.Highlight)
            peak = QColor(bar)
            peak.setAlpha(90)
            for i in range(n):
                x = left + i * bw
                for level, colour in ((self.peaks[i], peak), (self.levels[i], bar)):
                    if level:
                        bh = h * level / S_MAX
                        p.fillRect(int(x), int(top + h - bh), max(1, int(bw) - (1 if bw > 3 else 0)),
                                   int(bh) + 1, colour)
            if 0 <= self.cursor < n:
                p.setPen(QPen(QColor("#d04040"), 1))
                cx = int(left + (self.cursor + 0.5) * bw)
                p.drawLine(cx, top, cx, top + h)
            if self.marker is not None and self.freqs[0] <= self.marker <= self.freqs[-1]:
                span = (self.freqs[-1] - self.freqs[0]) or 1
                mx = int(left + (self.marker - self.freqs[0]) / span * (w - bw) + bw / 2)
                p.setPen(QPen(QColor("#30a030"), 2))
                p.drawLine(mx, top, mx, top + h)
            p.setPen(text)                               # frequency labels: ends and middle
            for i, align in ((0, Qt.AlignLeft), (n // 2, Qt.AlignHCenter), (n - 1, Qt.AlignRight)):
                x = left + (i + 0.5) * bw
                label = P.format_mhz(self.freqs[i], 4)
                fw = p.fontMetrics().horizontalAdvance(label)
                lx = {Qt.AlignLeft: left, Qt.AlignRight: left + w - fw}.get(align, x - fw / 2)
                p.drawText(int(lx), top + h + 14, label)
        p.setPen(grid)
        p.drawRect(left, top, w, h)
        p.end()

    def mouseMoveEvent(self, ev):
        i = self.index_at(ev.position().x())
        if i >= 0:
            self.hovered.emit(self.freqs[i], self.peaks[i])

    def mousePressEvent(self, ev):
        i = self.index_at(ev.position().x())
        if ev.button() == Qt.LeftButton and i >= 0:
            self.clicked.emit(self.freqs[i])


class ScanTab(QWidget):
    """Band sweep: step the VFO, read the S-meter, plot; click the plot to tune."""

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.saved = None                 # radio state from sweep_begin while sweeping
        self.running = False
        self.stop_requested = False
        self.tune_to: int | None = None
        self.index = 0
        self.t_sweep = 0.0
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Band"))
        self.band = QComboBox()
        self.band.addItems(P.BANDS)
        row.addWidget(self.band)
        row.addWidget(QLabel("From"))
        self.start_mhz = QLineEdit("144.000")
        self.start_mhz.setMaximumWidth(90)
        row.addWidget(self.start_mhz)
        row.addWidget(QLabel("to"))
        self.stop_mhz = QLineEdit("148.000")
        self.stop_mhz.setMaximumWidth(90)
        row.addWidget(self.stop_mhz)
        row.addWidget(QLabel("MHz  step"))
        self.step = QComboBox()
        self.step.addItems([f"{s:g}" for s in P.STEPS_KHZ])
        self.step.setCurrentText("25")
        row.addWidget(self.step)
        row.addWidget(QLabel("kHz  dwell"))
        self.dwell = QSpinBox()
        self.dwell.setRange(0, 2000)
        self.dwell.setSingleStep(25)
        self.dwell.setSuffix(" ms")
        self.dwell.setValue(SWEEP_DWELL_MS)
        self.dwell.setToolTip("Time for the receiver to settle at each frequency before the S-meter is read")
        row.addWidget(self.dwell)
        self.repeat = QCheckBox("Repeat")
        self.repeat.setToolTip("Sweep again and again until Stop; the faint bars hold the peaks")
        row.addWidget(self.repeat)
        self.go = QPushButton("Start")
        self.go.clicked.connect(self.toggle)
        row.addWidget(self.go)
        clear = QPushButton("Clear peaks")
        clear.clicked.connect(lambda: self.view.clear_peaks())
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        self.view = SpectrumView()
        self.view.clicked.connect(self.tune)
        self.view.hovered.connect(lambda hz, s: self.info.setText(
            f"{P.format_mhz(hz)} MHz  " + (f"S{s}" if s is not None else "-") + "   (click to tune)"))
        lay.addWidget(self.view, 1)
        self.info = QLabel("Sweeps the band's VFO and plots the S-meter. Click the graph to tune there.")
        lay.addWidget(self.info)

    # -- sweep ---------------------------------------------------------------
    def toggle(self):
        if self.running:
            self.stop_requested = True
            self.go.setEnabled(False)          # until the band is put back
        else:
            self.start()

    def start(self):
        try:
            freqs = P.sweep_frequencies(P.parse_frequency(self.start_mhz.text()),
                                        P.parse_frequency(self.stop_mhz.text()), self.step_khz)
        except ValueError as exc:
            self.main.error(exc)
            return
        if not freqs:
            self.main.error("no frequencies in that range")
            return
        if len(freqs) > SWEEP_MAX_POINTS:
            self.main.error(f"{len(freqs)} points: narrow the range or use a larger step "
                            f"(at most {SWEEP_MAX_POINTS})")
            return
        self.view.set_frequencies(freqs)
        self.running, self.stop_requested, self.tune_to, self.index = True, False, None, 0
        self.go.setText("Stop")
        self.band.setEnabled(False)
        band = self.band.currentIndex()
        self.main.run(lambda r: r.sweep_begin(band), self._begun, self._failed)

    @property
    def step_khz(self) -> float:
        return float(self.step.currentText())

    def _begun(self, saved):
        self.saved = saved
        self.t_sweep = time.monotonic()
        self._next()

    def _next(self):
        if self.stop_requested or self.main.radio is None:
            self._finish()
            return
        if self.index >= len(self.view.freqs):
            took = time.monotonic() - self.t_sweep
            self.info.setText(f"sweep of {len(self.view.freqs)} points took {took:.1f} s")
            if not self.repeat.isChecked():
                self._finish()
                return
            self.view.set_frequencies(self.view.freqs)   # keeps the peaks
            self.index, self.t_sweep = 0, time.monotonic()
        i, hz, saved = self.index, self.view.freqs[self.index], self.saved
        step, settle = self.step_khz, self.dwell.value() / 1000

        def done(level):
            self.view.set_level(i, level)
            self.index += 1
            self._next()
        self.main.run(lambda r: r.sweep_point(saved, hz, step, settle), done, self._failed)

    def _finish(self):
        saved, tune, step = self.saved, self.tune_to, self.step_khz
        if saved is None or self.main.radio is None:
            self._idle()
            return

        def done(_):
            self._idle()
            self.main.refresh_band(saved["band"])
            if tune is not None:
                self.info.setText(f"tuned band {P.BANDS[saved['band']]} to {P.format_mhz(tune)} MHz")
        self.main.run(lambda r: r.sweep_end(saved, tune, step), done, self._failed)

    def _failed(self, exc):
        self._idle()
        self.main.error(exc)

    def _idle(self):
        self.running = False
        self.saved = None
        self.view.cursor = -1
        self.view.update()
        self.go.setText("Start")
        self.go.setEnabled(True)
        self.band.setEnabled(True)

    # -- click to tune -------------------------------------------------------
    def tune(self, hz: int):
        self.view.marker = hz
        self.view.update()
        if self.running:                       # stop, and leave the band on hz
            self.tune_to = hz
            self.stop_requested = True
            self.go.setEnabled(False)
            return
        band, step = self.band.currentIndex(), self.step_khz

        def job(r):
            if r.get_band_mode(band) != 0:
                r.set_band_mode(band, 0)
            r.update_vfo(band, freq=hz, step=P.step_index(step))

        def done(_):
            self.main.refresh_band(band)
            self.info.setText(f"tuned band {P.BANDS[band]} to {P.format_mhz(hz)} MHz")
        self.main.run(job, done)


# ---------------------------------------------------------------------------
# memories
# ---------------------------------------------------------------------------

MEM_COLUMNS = [  # (header, CHIRP field or special, choices for a combo or None)
    ("Ch", "Location", None),
    ("Name", "Name", None),         # MEM_NAME_COLUMN
    ("Freq MHz", "Frequency", None),
    ("Duplex", "Duplex", ["", "+", "-", "split"]),
    ("Offset / TX MHz", "Offset", None),
    ("Tone", "Tone", ["", "Tone", "TSQL"]),
    ("Tone Hz", "rToneFreq", tone_items()),
    ("CTCSS Hz", "cToneFreq", tone_items()),
    ("Step kHz", "TStep", [f"{s:.2f}" for s in P.STEPS_KHZ]),
    ("Mode", "Mode", list(P.MODES.values())),
    ("Rev", "Rev", ["", "R"]),
    ("Skip", "Skip", ["", "S"]),
]


MEM_NAME_COLUMN = 1


class ComboDelegate(QStyledItemDelegate):
    def __init__(self, choices, parent=None):
        super().__init__(parent)
        self.choices = choices

    def createEditor(self, parent, option, index):
        c = QComboBox(parent)
        c.addItems(self.choices)
        return c

    def setEditorData(self, editor, index):
        i = editor.findText(index.data() or "")
        editor.setCurrentIndex(max(i, 0))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class NameDelegate(QStyledItemDelegate):
    """Memory name cells: the editor stops at what the radio can store."""

    def createEditor(self, parent, option, index):
        return limit_to_memory_name(QLineEdit(parent))


class MemoriesTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.ids = P.memory_ids()
        self.data: dict = {}         # memid -> Channel | None, as last read/written
        self.dirty: set = set()
        self.cancel = threading.Event()
        self._filling = False

        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.read_btn = QPushButton("Read all")
        self.read_btn.clicked.connect(lambda: self.read(self.ids))
        bar.addWidget(self.read_btn)
        b = QPushButton("Read selected")
        b.clicked.connect(lambda: self.read(self.selected_ids()))
        bar.addWidget(b)
        self.write_btn = QPushButton("Write changes")
        self.write_btn.clicked.connect(self.write_changes)
        bar.addWidget(self.write_btn)
        b = QPushButton("Erase selected")
        b.clicked.connect(self.erase_selected)
        bar.addWidget(b)
        bar.addSpacing(16)
        for band in (0, 1):
            b = QPushButton(f"Recall → {P.BANDS[band]}")
            b.clicked.connect(lambda _=False, band=band: self.recall(band))
            bar.addWidget(b)
        bar.addSpacing(16)
        b = QPushButton("Import CSV…")
        b.clicked.connect(self.import_csv)
        bar.addWidget(b)
        b = QPushButton("Export CSV…")
        b.clicked.connect(self.export_csv)
        bar.addWidget(b)
        bar.addStretch(1)
        self.hide_empty = QCheckBox("Hide empty")
        self.hide_empty.toggled.connect(self.apply_filter)
        bar.addWidget(self.hide_empty)
        lay.addLayout(bar)

        prog = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        prog.addWidget(self.progress, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel.set)
        prog.addWidget(self.cancel_btn)
        lay.addLayout(prog)

        self.table = QTableWidget(len(self.ids), len(MEM_COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in MEM_COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(MEM_NAME_COLUMN, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 8)
        for col, (_h, _f, choices) in enumerate(MEM_COLUMNS):
            if choices:
                self.table.setItemDelegateForColumn(col, ComboDelegate(choices, self.table))
        self.table.setItemDelegateForColumn(MEM_NAME_COLUMN, NameDelegate(self.table))
        self._filling = True
        for row, memid in enumerate(self.ids):
            label = P.CALL_LABELS.get(memid, memid)
            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, memid)
            self.table.setItem(row, 0, item)
            for col in range(1, len(MEM_COLUMNS)):
                self.table.setItem(row, col, QTableWidgetItem(""))
        self._filling = False
        self.table.itemChanged.connect(self._item_changed)
        lay.addWidget(self.table)
        self.hint = QLabel("Double-click a cell to edit; changed rows turn yellow until written. "
                           "Duplex 'split' takes the transmit frequency in the Offset column.")
        self.hint.setStyleSheet("color: gray")
        lay.addWidget(self.hint)

    # -- table <-> channels ------------------------------------------------
    def row_of(self, memid) -> int:
        return self.ids.index(memid)

    def selected_ids(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.ids[r] for r in rows]

    def update_row(self, memid, ch, clean=True):
        if clean:
            self.data[memid] = ch
            self.dirty.discard(memid)
            for panel in self.main.radio_tab.panels:     # their memory lists show names
                relabel_memory_combo(panel.mem_combo, self.data, memid)
                if panel.band_mode == 2 and panel.mem_combo.currentData() == memid:
                    self.main.refresh_band(panel.band)   # its display line shows this memory
        row = self.row_of(memid)
        self._filling = True
        try:
            if ch is None:
                values = [""] * (len(MEM_COLUMNS) - 1)
            else:
                d = memfile.channel_to_row(memid, ch)
                d["Rev"] = "R" if ch.reverse else ""
                if not ch.tone:
                    d["rToneFreq"] = ""      # shown only where it is used
                if not ch.ctcss:
                    d["cToneFreq"] = ""
                if ch.duplex == "":
                    d["Offset"] = ""
                values = [d[f] for _h, f, _c in MEM_COLUMNS[1:]]
                values[1] = f"{ch.freq / 1e6:.5f}"
                if values[3]:
                    values[3] = f"{float(values[3]):.5f}"
            for col, v in enumerate(values, start=1):
                item = self.table.item(row, col)
                item.setText(v)
                item.setBackground(QColor(0, 0, 0, 0))
                item.setToolTip("")
            self.table.item(row, 0).setBackground(QColor(0, 0, 0, 0))
        finally:
            self._filling = False
        self.apply_filter()

    def channel_from_row(self, memid):
        row = self.row_of(memid)
        d = {f: self.table.item(row, c).text().strip() for c, (_h, f, _ch) in enumerate(MEM_COLUMNS)}
        d["Name"] = self.table.item(row, MEM_NAME_COLUMN).text()   # the radio keeps spaces in names
        d["Location"] = memid
        if not d["Frequency"]:
            return None
        if d["Duplex"] in ("+", "-") and not d["Offset"]:
            d["Offset"] = "0.6" if float(d["Frequency"]) < 300 else "5.0"
        _m, ch, _w = memfile.row_to_channel(d)
        ch.reverse = d["Rev"] == "R"
        return ch

    def _item_changed(self, item):
        if self._filling or item.column() == 0:
            return
        memid = self.ids[item.row()]
        row = item.row()
        try:
            self.channel_from_row(memid)
            color, tip = DIRTY, "changed - not yet written"
        except (ValueError, KeyError) as exc:
            color, tip = INVALID, f"invalid: {exc}"
        self.dirty.add(memid)
        self._filling = True
        for col in range(len(MEM_COLUMNS)):
            self.table.item(row, col).setBackground(color)
            self.table.item(row, col).setToolTip(tip)
        self._filling = False
        self.write_btn.setText(f"Write changes ({len(self.dirty)})")

    def apply_filter(self):
        hide = self.hide_empty.isChecked()
        for row, memid in enumerate(self.ids):
            empty = self.data.get(memid) is None and memid not in self.dirty
            self.table.setRowHidden(row, hide and empty)

    # -- radio operations -----------------------------------------------------
    def _busy(self, on, total=0):
        self.progress.setVisible(on)
        self.cancel_btn.setVisible(on)
        self.read_btn.setEnabled(not on)
        self.write_btn.setEnabled(not on)
        if on:
            self.progress.setRange(0, total)
            self.progress.setValue(0)
            self.cancel.clear()

    def read(self, ids):
        if not ids:
            return
        ids = list(ids)
        self._busy(True, len(ids))

        def job(progress):
            for i, memid in enumerate(ids):
                if self.cancel.is_set():
                    break
                progress((i + 1, memid, self.main.radio.read_memory(memid)))

        def step(v):
            n, memid, ch = v
            self.progress.setValue(n)
            self.progress.setFormat(f"reading {memid}  %v/%m")
            self.update_row(memid, ch)

        self.main.run_job(job, on_done=lambda _: self._busy(False),
                          on_error=lambda e: (self._busy(False), self.main.error(e)), on_progress=step)

    def write_changes(self):
        try:
            todo = [(m, self.channel_from_row(m)) for m in sorted(self.dirty, key=self.ids.index)]
        except (ValueError, KeyError) as exc:
            self.main.error(f"fix the red rows first: {exc}")
            return
        if not todo:
            self.main.status("nothing to write")
            return
        self._busy(True, len(todo))

        def job(progress):
            for i, (memid, ch) in enumerate(todo):
                if self.cancel.is_set():
                    break
                if ch is None:
                    if not P.is_call(memid):
                        self.main.radio.erase_memory(memid)
                    back = None
                else:
                    self.main.radio.write_memory(memid, ch)
                    back = self.main.radio.read_memory(memid)
                progress((i + 1, memid, back))

        def step(v):
            n, memid, ch = v
            self.progress.setValue(n)
            self.progress.setFormat(f"writing {memid}  %v/%m")
            self.update_row(memid, ch)
            self.write_btn.setText(f"Write changes ({len(self.dirty)})" if self.dirty else "Write changes")

        self.main.run_job(job, on_done=lambda _: (self._busy(False), self.main.status("memories written")),
                          on_error=lambda e: (self._busy(False), self.main.error(e)), on_progress=step)

    def erase_selected(self):
        ids = [m for m in self.selected_ids() if not P.is_call(m)]
        if not ids:
            return
        if QMessageBox.question(self, "Erase memories",
                                f"Erase {len(ids)} memor{'y' if len(ids) == 1 else 'ies'} "
                                f"({', '.join(ids[:8])}{'…' if len(ids) > 8 else ''}) on the radio?") \
                != QMessageBox.Yes:
            return

        def job():
            for memid in ids:
                self.main.radio.erase_memory(memid)
            return ids

        self.main.run(lambda r: job(), lambda done: [self.update_row(m, None) for m in done])

    def recall(self, band):
        ids = self.selected_ids()
        if not ids:
            return
        memid = ids[0]
        self.main.run(lambda r: r.recall_memory(band, memid), lambda _: self.main.refresh_band(band))

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import memories", "", "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            mems, warnings = memfile.import_csv(path)
        except (OSError, ValueError) as exc:
            self.main.error(exc)
            return
        for memid, ch in mems.items():
            self.update_row(memid, ch, clean=False)
            self._item_changed(self.table.item(self.row_of(memid), 1))
        msg = f"{len(mems)} memories loaded from {os.path.basename(path)} - review, then Write changes"
        if warnings:
            QMessageBox.information(self, "Import", msg + "\n\n" + "\n".join(warnings[:30]))
        self.main.status(msg)

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export memories", "d7-memories.csv", "CSV files (*.csv)")
        if not path:
            return
        mems = {}
        for memid in self.ids:
            try:
                mems[memid] = self.channel_from_row(memid)
            except (ValueError, KeyError):
                mems[memid] = self.data.get(memid)
        n = memfile.export_csv(path, mems)
        self.main.status(f"{n} memories exported to {path}")


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

class SettingRow:
    def __init__(self, s, tab: "SettingsTab"):
        self.s, self.tab = s, tab
        self.raw = None
        self.label = QLabel(s.label)
        self.label.setToolTip(f"{s.key}: {s.describe_values()}" + (f"\n{s.note}" if s.note else ""))
        k = s.kind
        if k == "bool":
            w = QCheckBox()
            w.clicked.connect(self.changed)
        elif k in ("choice", "map"):
            w = QComboBox()
            w.addItems(list(s.choices) if k == "choice" else [l for l, _ in s.choices])
            if s.key == "ICO":
                w.setEditable(True)
            w.activated.connect(self.changed)
            if w.isEditable():
                w.lineEdit().editingFinished.connect(self.changed)
        elif k == "int":
            w = QSpinBox()
            w.setRange(s.minimum, s.maximum)
            w.valueChanged.connect(self.changed)
        else:
            w = QLineEdit()
            if s.max_len:
                w.setMaxLength(s.max_len)
            w.setPlaceholderText(s.describe_values())
            w.textEdited.connect(self.changed)
        w.setToolTip(self.label.toolTip())
        self.widget = w
        self.set_available(False, "not read yet")

    def set_available(self, ok: bool, why: str = ""):
        self.widget.setEnabled(ok)
        if not ok and why:
            self.widget.setToolTip(f"{why}\n{self.label.toolTip()}")

    def show_raw(self, raw):
        self.raw = raw
        if raw is None:
            self.set_available(False, "not available on this radio now" + (f" - {self.s.note}" if self.s.note else ""))
            return
        self.set_available(True)
        try:
            value = self.s.decode(raw)
        except ValueError:
            value = raw                    # unexpected format: show it as the radio sent it
        w = self.widget
        w.blockSignals(True)
        try:
            if isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            elif isinstance(w, QComboBox):
                i = w.findText(str(value))
                if i >= 0:
                    w.setCurrentIndex(i)
                elif w.isEditable():
                    w.setEditText(str(value))
            elif isinstance(w, QSpinBox):
                w.setValue(int(value))
            else:
                w.setText(str(value))
        finally:
            w.blockSignals(False)
        self.mark(False)

    def value(self):
        w = self.widget
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, QSpinBox):
            return w.value()
        return w.text()

    def new_raw(self):
        return self.s.encode(self.value())

    def changed(self, *_):
        if self._filling():
            return
        try:
            dirty = self.raw is not None and self.new_raw() != self.raw
            self.label.setStyleSheet("font-weight: bold; color: #a06000;" if dirty else "")
            self.widget.setStyleSheet("")
        except ValueError as exc:
            dirty = True
            self.label.setStyleSheet("font-weight: bold; color: #c00000;")
            self.widget.setToolTip(str(exc))
        if dirty:
            self.tab.dirty.add(self.s.key)
        else:
            self.tab.dirty.discard(self.s.key)
        self.tab.update_buttons()

    def mark(self, dirty):
        self.label.setStyleSheet("font-weight: bold; color: #a06000;" if dirty else "")

    def _filling(self):
        return self.tab.filling


class SettingsTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.rows: dict = {}
        self.dirty: set = set()
        self.filling = False
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.read_btn = QPushButton("Read all")
        self.read_btn.clicked.connect(self.read_all)
        bar.addWidget(self.read_btn)
        self.write_btn = QPushButton("Write changes")
        self.write_btn.clicked.connect(self.write_changes)
        bar.addWidget(self.write_btn)
        b = QPushButton("Revert")
        b.clicked.connect(self.revert)
        bar.addWidget(b)
        bar.addSpacing(16)
        b = QPushButton("Backup radio…")
        b.setToolTip("Save every memory and setting to a JSON file")
        b.clicked.connect(main.backup)
        bar.addWidget(b)
        b = QPushButton("Restore…")
        b.clicked.connect(main.restore)
        bar.addWidget(b)
        bar.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        bar.addWidget(self.progress)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.build(None)

    def build(self, model):
        self.tabs.clear()
        self.rows.clear()
        self.dirty.clear()
        for group in GROUPS:
            items = [s for s in settings_for(model) if s.group == group]
            if not items:
                continue
            page = QWidget()
            grid = QGridLayout(page)
            half = (len(items) + 1) // 2 if len(items) > 12 else len(items)
            for i, s in enumerate(items):
                row = SettingRow(s, self)
                self.rows[s.key] = row
                r, c = (i, 0) if i < half else (i - half, 2)
                grid.addWidget(row.label, r, c)
                grid.addWidget(row.widget, r, c + 1)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)
            grid.setRowStretch(half, 1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, group)
        self.update_buttons()

    def update_buttons(self):
        self.write_btn.setText(f"Write changes ({len(self.dirty)})" if self.dirty else "Write changes")

    def show_values(self, raws: dict):
        self.filling = True
        try:
            for key, raw in raws.items():
                if key in self.rows:
                    self.rows[key].show_raw(raw)
                    self.dirty.discard(key)
        finally:
            self.filling = False
        self.update_buttons()

    def read_all(self):
        items = list(self.rows)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(items))

        def job(progress):
            radio = self.main.radio
            out = {}
            for i, s in enumerate(radio.settings()):
                try:
                    out[s.key] = radio.read_setting_raw(s)
                except (CommandRejected, ValueError):
                    out[s.key] = None
                progress(i + 1)
            return out

        def done(raws):
            self.progress.setVisible(False)
            self.show_values(raws)
            self.main.status(f"{sum(v is not None for v in raws.values())} settings read")

        self.main.run_job(job, on_done=done, on_progress=self.progress.setValue,
                          on_error=lambda e: (self.progress.setVisible(False), self.main.error(e)))

    def write_changes(self):
        todo = []
        for key in list(self.dirty):
            row = self.rows[key]
            try:
                todo.append((row.s, row.new_raw()))
            except ValueError as exc:
                self.main.error(f"{row.s.label}: {exc}")
                return
        if any(s.key == "TNC" and raw == "1" for s, raw in todo):
            if QMessageBox.warning(self, "TNC", "Turning the TNC on in PACKET mode hands the serial "
                                   "port to the TNC and this program loses control. Continue?",
                                   QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return

        def job():
            radio = self.main.radio
            out = {}
            for s, raw in todo:
                radio.write_setting_raw(s, raw)
                out[s.key] = radio.read_setting_raw(s)
            return out

        self.main.run(lambda r: job(), lambda raws: (self.show_values(raws), self.main.refresh_globals(),
                                                     self.main.status(f"{len(raws)} settings written")))

    def revert(self):
        self.show_values({k: r.raw for k, r in self.rows.items() if r.raw is not None})
        for r in self.rows.values():
            r.mark(False)
        self.dirty.clear()
        self.update_buttons()


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------

class ConsoleTab(QWidget):
    METER_TRAFFIC = ("SM ", "BY ")

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font(10))
        self.log.setMaximumBlockCount(CONSOLE_LINES)
        lay.addWidget(self.log)
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setFont(mono_font(10))
        self.entry.setPlaceholderText("raw command, e.g.  BUF 0   MR 0,0,005   SQ 1,03   (Enter to send)")
        self.entry.returnPressed.connect(self.send)
        row.addWidget(self.entry, 1)
        b = QPushButton("Send")
        b.clicked.connect(self.send)
        row.addWidget(b)
        self.quiet = QCheckBox("Hide S-meter / busy traffic")
        self.quiet.setChecked(True)
        row.addWidget(self.quiet)
        b = QPushButton("Clear")
        b.clicked.connect(self.log.clear)
        row.addWidget(b)
        lay.addLayout(row)

    def add(self, direction, line):
        if self.quiet.isChecked() and line.startswith(self.METER_TRAFFIC):
            return
        line = line.rstrip("\r\n")
        spaces = len(line) - len(line.rstrip(" "))
        # trailing spaces are data (memory names) but invisible in the log: count them
        mark = f"   [{spaces} trailing space{'s' * (spaces > 1)}]" if spaces else ""
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} {direction} {line}{mark}")

    def note(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} # {text}")

    def send(self):
        line = self.entry.text().lstrip()     # trailing spaces are data (memory names)
        if not line.strip():
            return
        self.entry.clear()
        # a raw command can change anything: refresh the band displays afterwards
        self.main.run(lambda r: r.command(line, check=False), lambda _: self.main.refresh_soon.start())


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, port=None, store: QSettings | None = None):
        """store: where the window layout is remembered between runs (None: not kept)."""
        super().__init__()
        self.store = store
        self.setWindowTitle("TH-D7 Control")
        self.radio: D7Radio | None = None
        self.transmitting = False
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.worker = Worker(self.error)

        tb = self.addToolBar("Connection")
        tb.setMovable(False)
        tb.addWidget(QLabel(" Port "))
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(220)
        tb.addWidget(self.port)
        act = QAction("↻", self)
        act.setToolTip("Rescan serial ports")
        act.triggered.connect(self.scan_ports)
        tb.addAction(act)
        tb.addWidget(QLabel(" Baud "))
        self.baud = QComboBox()
        self.baud.addItems(["9600", "4800", "19200"])
        tb.addWidget(self.baud)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        tb.addWidget(self.connect_btn)
        tb.addSeparator()
        self.live = QCheckBox("Live updates (AI)")
        self.live.setToolTip("Ask the radio to report changes as they happen (auto-information)")
        self.live.setChecked(True)
        self.live.clicked.connect(self._live_changed)
        tb.addWidget(self.live)
        self.poll = QCheckBox("Poll meters")
        self.poll.setToolTip("Read S-meter and busy for both bands every second.\n"
                             "Untick it to use the radio's menus: a command from the PC "
                             "cancels menu mode on the TH-D7A.")
        self.poll.setChecked(True)
        tb.addWidget(self.poll)
        tb.addSeparator()
        self.model_label = QLabel("  not connected")
        tb.addWidget(self.model_label)

        # Every panel is a dock: drag its title bar to put panels side by
        # side or in tabs, or double-click it to float it as its own window.
        self.radio_tab = RadioTab(self)
        self.memories = MemoriesTab(self)
        self.settings_tab = SettingsTab(self)
        self.console = ConsoleTab(self)
        self.scan_tab = ScanTab(self)
        self.bridge.traffic.connect(self.console.add)
        self.docks = {}
        view = self.menuBar().addMenu("&View")
        for name, panel in (("Radio", self.radio_tab), ("Memories", self.memories),
                            ("Settings", self.settings_tab), ("Scan", self.scan_tab),
                            ("Console", self.console)):
            dock = QDockWidget(name, self)
            dock.setObjectName(f"dock_{name}")          # saveState keys on it
            dock.setWidget(panel)
            self.docks[name] = dock
            view.addAction(dock.toggleViewAction())
        view.addSeparator()
        view.addAction("Reset layout", self.default_layout)
        self.menuBar().addMenu("&Help").addAction("&About d7ctl", self.about)
        self.setDockNestingEnabled(True)
        self.default_layout()
        self.set_connected(False)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(POLL_MS)
        self.refresh_soon = QTimer(self)
        self.refresh_soon.setSingleShot(True)
        self.refresh_soon.setInterval(250)
        self.refresh_soon.timeout.connect(self.refresh_all)
        self.tx_guard = QTimer(self)
        self.tx_guard.setSingleShot(True)
        self.tx_guard.timeout.connect(lambda: (self.key_tx(False), self.error("TX timeout - unkeyed")))

        self.scan_ports()
        if port:
            self.port.setCurrentText(port)
        self.resize(1180, 760)
        if self.store is not None and self.store.value("window/geometry") is not None:
            self.restoreGeometry(self.store.value("window/geometry"))
            self.restoreState(self.store.value("window/state"))

    def about_text(self) -> str:
        import PySide6
        import serial
        from .version import build_info
        i = build_info()
        if i["commit"] is None:
            commit = "not a git checkout"
        else:
            commit = i["commit"] + ("+ (uncommitted changes)" if i["dirty"] else "")
        return (f"<h3>d7ctl {i['version']}</h3>"
                "<p>Control for the Kenwood TH-D7 / TH-D7G over its serial port.</p>"
                f"<table><tr><td>Git commit&nbsp;</td><td><tt>{commit}</tt></td></tr>"
                f"<tr><td>Date</td><td>{i['date'] or '-'}</td></tr></table>"
                f"<p><small>Python {sys.version.split()[0]}, PySide6 {PySide6.__version__}, "
                f"pyserial {serial.__version__}</small></p>")

    def about(self):
        QMessageBox.about(self, "About d7ctl", self.about_text())

    CONSOLE_HEIGHT = 180          # px: about ten lines of traffic under the panels

    def default_layout(self):
        """Radio, Memories, Settings and Scan in tabs at the top; the Console below them."""
        d = self.docks
        for dock in d.values():
            dock.setFloating(False)
            dock.show()
        self.addDockWidget(Qt.TopDockWidgetArea, d["Radio"])
        self.addDockWidget(Qt.BottomDockWidgetArea, d["Console"])
        self.tabifyDockWidget(d["Radio"], d["Memories"])
        self.tabifyDockWidget(d["Radio"], d["Settings"])
        self.tabifyDockWidget(d["Radio"], d["Scan"])
        d["Radio"].raise_()
        self.resizeDocks([d["Console"]], [self.CONSOLE_HEIGHT], Qt.Vertical)

    # -- plumbing -----------------------------------------------------------
    def status(self, text):
        self.statusBar().setStyleSheet("")
        self.statusBar().showMessage(str(text), 6000)

    def error(self, exc):
        text = str(exc) or exc.__class__.__name__
        self.statusBar().setStyleSheet("color: #b00000;")
        self.statusBar().showMessage(text, 10000)
        self.console.note(f"error: {text}")

    def run(self, fn, on_done=None, on_error=None):
        """Queue fn(radio) on the worker; on_done(result) runs in the GUI thread."""
        if self.radio is None:
            self.error("not connected")
            return
        radio = self.radio
        self.worker.submit(lambda: fn(radio), on_done, on_error)

    def run_job(self, fn, on_done=None, on_error=None, on_progress=None):
        if self.radio is None:
            self.error("not connected")
            return
        self.worker.submit(fn, on_done, on_error, on_progress)

    def scan_ports(self):
        current = self.port.currentText()
        self.port.clear()
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                self.port.addItem(p.device)
        except Exception:
            pass
        self.port.addItem("emulator")
        if current:
            self.port.setCurrentText(current)
        elif self.port.count() > 1:
            self.port.setCurrentIndex(0)
        else:
            self.port.setCurrentText(os.environ.get("D7_PORT", "/dev/ttyUSB0"))

    def set_connected(self, on: bool):
        for w in (self.radio_tab, self.memories, self.settings_tab, self.scan_tab):
            w.setEnabled(on)
        self.console.entry.setEnabled(on)
        self.port.setEnabled(not on)
        self.baud.setEnabled(not on)
        self.connect_btn.setText("Disconnect" if on else "Connect")

    # -- connection -------------------------------------------------------------
    def toggle_connection(self):
        if self.radio is not None:
            self.disconnect_radio()
            return
        port = self.port.currentText().strip()
        radio = D7Radio(port, baudrate=int(self.baud.currentText()))
        radio.add_event_listener(self.bridge.event.emit)
        radio.add_traffic_listener(self.bridge.traffic.emit)
        live = self.live.isChecked()
        self.connect_btn.setEnabled(False)
        self.model_label.setText(f"  connecting to {port}…")

        def opened(model):
            self.connect_btn.setEnabled(True)
            self.radio = radio
            self.model_label.setText(f"  {model} on {port}")
            self.console.note(f"connected: {model} on {port}")
            self.set_connected(True)
            self.settings_tab.build(model)
            self.check_1750()
            self.refresh_all()
            self.settings_tab.read_all()

        def failed(exc):
            self.connect_btn.setEnabled(True)
            self.model_label.setText("  not connected")
            self.error(f"cannot connect on {port}: {exc}")

        self.worker.submit(lambda: radio.open(auto_info=live), opened, failed)

    def check_1750(self):
        """Grey out the 1750 Hz button on radios without the tone. The TH-D7A and
        TH-D7E both report ID TH-D7, so ask: the A answers N to TH (1750 Hz hold)
        and to TT itself (2026-10-27). TH is only a read, nothing transmits."""
        def done(ok):
            b = self.radio_tab.t1750
            b.setEnabled(ok)
            b.setToolTip("Transmits a 1750 Hz tone (repeater access) while held" if ok else
                         "This radio has no 1750 Hz tone (it refuses TH and TT)")
        self.run(lambda r: r.command("TH", check=False) not in P.ERROR_REPLIES, done)

    def disconnect_radio(self):
        radio, self.radio = self.radio, None
        if radio is None:
            return
        self.set_connected(False)
        self.model_label.setText("  not connected")

        def close():
            try:
                if self.transmitting:
                    radio.transmit(False)
                radio.set_auto_info(False)
            except RadioError:
                pass
            radio.close()

        self.transmitting = False
        self.worker.submit(close, lambda _: self.console.note("disconnected"))

    def closeEvent(self, ev):
        if self.store is not None:
            self.store.setValue("window/geometry", self.saveGeometry())
            self.store.setValue("window/state", self.saveState())
        self.disconnect_radio()
        self.worker.pool.waitForDone(3000)
        super().closeEvent(ev)

    def _live_changed(self, on):
        self.run(lambda r: r.set_auto_info(on))

    # -- refresh -----------------------------------------------------------------
    def refresh_all(self, *_):
        self.refresh_band(0)
        self.refresh_band(1)
        self.refresh_globals()

    def refresh_band(self, band, *_):
        def job(r):
            try:
                return r.band_status(band)
            except CommandRejected:
                return {"band": P.BANDS[band], "hidden": True}
        self.run(job, self.radio_tab.panels[band].show_status)

    def refresh_globals(self, *_):
        def job(r):
            out = {"BC": r.get_control_band()}
            for key in list(self.radio_tab.toggles) + ["BAL"]:
                s = next((s for s in r.settings() if s.key == key), None)
                if s is None:
                    continue
                try:
                    out[key] = r.read_setting_raw(s)
                except (CommandRejected, ValueError):
                    out[key] = None
            return out
        self.run(job, self._show_globals)

    def _show_globals(self, g):
        for i, p in enumerate(self.radio_tab.panels):
            p.set_control(g["BC"] == i)
        for key, cb in self.radio_tab.toggles.items():
            raw = g.get(key)
            cb.setEnabled(raw is not None)
            cb.setChecked(raw == "1")
        if g.get("BAL") is not None:
            self.radio_tab.balance.setCurrentIndex(int(g["BAL"]))

    def set_setting_value(self, key, value, then=None):
        def job(r):
            s = next(s for s in r.settings() if s.key == key)
            r.write_setting(s, value)
            return r.read_setting_raw(s)
        self.run(job, lambda raw: (self.settings_tab.show_values({key: raw}), then and then()))

    def _poll(self):
        if (self.radio is None or not self.poll.isChecked() or self.worker.pending
                or self.transmitting or self.scan_tab.running):
            return

        def job(r):
            out = []
            for b in (0, 1):
                try:
                    out.append((r.get_smeter(b), r.get_busy(b)))
                except CommandRejected:
                    out.append(None)
            return out

        def show(vals):
            for p, v in zip(self.radio_tab.panels, vals):
                if v is not None:
                    p.smeter.setValue(v[0])
                    p.set_busy(v[1])

        self.run(job, show)

    # -- transmit -------------------------------------------------------------------
    def key_tx(self, on: bool, how: str = "TX"):
        if self.radio is None:
            return
        if on:
            self.transmitting = True
            self.tx_guard.start(TX_TIMEOUT_MS)
            self.run(lambda r: r.command(how), lambda _: self.status("TRANSMITTING"))
        else:
            self.tx_guard.stop()
            self.transmitting = False
            self.run(lambda r: r.transmit(False), lambda _: self.status("receive"))

    # -- auto-info --------------------------------------------------------------------
    def on_event(self, line: str):
        mn, args = P.split_reply(line)
        try:
            if mn == "BUF":
                b, ch = P.parse_buf(line)
                self.radio_tab.panels[b].show_channel(ch)
            elif mn == "SM" and len(args) == 2:
                self.radio_tab.panels[int(args[0])].smeter.setValue(int(args[1]))
            elif mn == "BY" and len(args) == 2:
                self.radio_tab.panels[int(args[0])].set_busy(args[1] == "1")
            elif mn in ("BC", "VMC", "MC", "DL"):
                self.refresh_soon.start()     # coalesce a burst of reports
            elif mn == "MNA" and len(args) >= 3:
                # a name entered on the radio's keys ([F], [9]); the TH-D7A reports it (2026-10-28)
                self.memory_renamed(P.normalise_memid(args[1]), ",".join(args[2:]))
        except (ValueError, IndexError, P.ProtocolError):
            pass

    def memory_renamed(self, memid: str, name: str):
        mt = self.memories
        ch = mt.data.get(memid)
        if ch is None:                         # not read yet (or thought empty): read it whole
            self.run(lambda r: r.read_memory(memid), lambda c: mt.update_row(memid, c))
        elif memid in mt.dirty:                # keep the user's unwritten edits in the row
            ch.name = name
        else:
            mt.update_row(memid, dataclasses.replace(ch, name=name))

    # -- backup / restore ---------------------------------------------------------------
    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, "Backup radio", "d7-backup.json", "JSON (*.json)")
        if not path:
            return
        bar = self.settings_tab.progress
        bar.setVisible(True)
        bar.setRange(0, len(P.memory_ids()))

        def job(progress):
            r = self.radio
            mems = r.read_all_memories(progress=lambda n, t, m: progress(n))
            settings = r.read_all_settings()
            memfile.write_backup(path, r.model, mems, settings)
            return mems, settings

        def done(v):
            bar.setVisible(False)
            mems, settings = v
            for memid, ch in mems.items():
                self.memories.update_row(memid, ch)
            self.settings_tab.show_values(settings)
            self.status(f"backup saved to {path}")

        self.run_job(job, done, lambda e: (bar.setVisible(False), self.error(e)), bar.setValue)

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "Restore radio", "", "JSON (*.json)")
        if not path:
            return
        try:
            doc = memfile.read_backup(path)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        if QMessageBox.question(
                self, "Restore", f"Write {len(doc['memories'])} memories and {len(doc['settings'])} "
                f"settings from a {doc['model']} backup ({doc.get('saved', '?')}) to the radio?\n"
                "Memories empty in the backup are erased.") != QMessageBox.Yes:
            return

        def job(progress):
            r = self.radio
            by_key = {s.key: s for s in r.settings()}
            total = len(doc["memories"]) + len(doc["settings"])
            n = 0
            for memid, ch in doc["memories"].items():
                r.write_memory(memid, ch)
                n += 1
                progress(n)
            for memid in doc.get("empty_memories", []):
                if not P.is_call(memid):
                    r.erase_memory(memid)
            refused = []
            for key, raw in doc["settings"].items():
                s = by_key.get(key)
                n += 1
                progress(n)
                if s is None or not s.restore:
                    continue
                try:
                    r.write_setting_raw(s, raw)
                except RadioError:
                    refused.append(key)
            return total, refused

        bar = self.settings_tab.progress
        bar.setVisible(True)
        bar.setRange(0, len(doc["memories"]) + len(doc["settings"]))

        def done(v):
            bar.setVisible(False)
            _total, refused = v
            self.status("restored" + (f"; refused: {', '.join(refused)}" if refused else ""))
            self.memories.read(P.memory_ids())
            self.settings_tab.read_all()
            self.refresh_all()

        self.run_job(job, done, lambda e: (bar.setVisible(False), self.error(e)), bar.setValue)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kenwood TH-D7 control (GUI)")
    ap.add_argument("--port", help="serial port to preselect (or 'emulator')")
    ap.add_argument("--connect", action="store_true", help="connect at start-up")
    args, qt_args = ap.parse_known_args(argv)
    app = QApplication.instance() or QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("d7ctl")
    win = MainWindow(args.port, QSettings("d7ctl", "d7ctl"))
    win.show()
    if args.connect:
        QTimer.singleShot(0, win.toggle_connection)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
