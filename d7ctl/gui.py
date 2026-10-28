"""d7ctl-gui - graphical control of a Kenwood TH-D7 / TH-D7G.

Tabs: Radio (both bands, PTT, global switches), Memories (all 222
channels, edit in place, CSV import/export), Settings (every menu
setting), Console (serial traffic and raw commands).

All radio I/O runs on one worker thread, in order, so the window never
blocks; auto-info reports from the radio arrive through Qt signals.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSlider, QSpinBox, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
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
        self.mem_combo = QComboBox()
        self.mem_combo.addItems([m for m in P.memory_ids() if not P.is_call(m)])
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
        memid = self.mem_combo.currentText()
        b = self.band
        self.main.run(lambda r: r.recall_memory(b, memid), lambda _: self.main.refresh_band(b))

    def _store(self):
        memid, ok = QInputDialog.getItem(self, "Store VFO", f"Store band {P.BANDS[self.band]} in memory:",
                                         [m for m in P.memory_ids() if not P.is_call(m)], 0, False)
        if not ok:
            return
        name, ok = QInputDialog.getText(self, "Store VFO", f"Name for {memid} (up to {P.NAME_LEN}):")
        if not ok:
            return
        b = self.band
        self.main.run(lambda r: r.vfo_to_memory(b, memid, name[:P.NAME_LEN]),
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
            i = self.mem_combo.findText(st["memory"])
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
        self.show_channel(st["vfo"], st.get("memory"))

    def show_channel(self, ch: P.Channel, memid=None):
        self.ch = ch
        self.display.setText(f"{ch.freq / 1e6:.5f}")
        mode = P.BAND_MODES.get(self.band_mode, "")
        where = f"{mode} {self.mem_combo.currentText()}" if self.band_mode == 2 else mode
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
# memories
# ---------------------------------------------------------------------------

MEM_COLUMNS = [  # (header, CHIRP field or special, choices for a combo or None)
    ("Ch", "Location", None),
    ("Name", "Name", None),
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
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 8)
        for col, (_h, _f, choices) in enumerate(MEM_COLUMNS):
            if choices:
                self.table.setItemDelegateForColumn(col, ComboDelegate(choices, self.table))
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
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} {direction} {line}")

    def note(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')} # {text}")

    def send(self):
        line = self.entry.text().strip()
        if not line:
            return
        self.entry.clear()
        self.main.run(lambda r: r.command(line, check=False))


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
        self.bridge.traffic.connect(self.console.add)
        self.docks = {}
        view = self.menuBar().addMenu("&View")
        for name, panel in (("Radio", self.radio_tab), ("Memories", self.memories),
                            ("Settings", self.settings_tab), ("Console", self.console)):
            dock = QDockWidget(name, self)
            dock.setObjectName(f"dock_{name}")          # saveState keys on it
            dock.setWidget(panel)
            self.docks[name] = dock
            view.addAction(dock.toggleViewAction())
        view.addSeparator()
        view.addAction("Reset layout", self.default_layout)
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

    CONSOLE_HEIGHT = 180          # px: about ten lines of traffic under the panels

    def default_layout(self):
        """Radio, Memories and Settings in tabs at the top; the Console below them."""
        d = self.docks
        for dock in d.values():
            dock.setFloating(False)
            dock.show()
        self.addDockWidget(Qt.TopDockWidgetArea, d["Radio"])
        self.addDockWidget(Qt.BottomDockWidgetArea, d["Console"])
        self.tabifyDockWidget(d["Radio"], d["Memories"])
        self.tabifyDockWidget(d["Radio"], d["Settings"])
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
        for w in (self.radio_tab, self.memories, self.settings_tab):
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
            self.refresh_all()
            self.settings_tab.read_all()

        def failed(exc):
            self.connect_btn.setEnabled(True)
            self.model_label.setText("  not connected")
            self.error(f"cannot connect on {port}: {exc}")

        self.worker.submit(lambda: radio.open(auto_info=live), opened, failed)

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
        if self.radio is None or not self.poll.isChecked() or self.worker.pending or self.transmitting:
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
        except (ValueError, IndexError, P.ProtocolError):
            pass

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
