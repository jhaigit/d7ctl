import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtCore = pytest.importorskip("PySide6.QtCore")

from d7ctl.gui import MainWindow  # noqa: E402


def pump(app, win, cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_gui_connect_edit_and_memories(app):
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    a = win.radio_tab.panels[0]
    assert a.display.text() == "144.39000"
    # set a frequency from the entry box
    a.freq_edit.setText("145.6")
    a._set_freq()
    assert pump(app, win, lambda: a.display.text() == "145.60000")
    # tone mode via the combo
    a._edit(tone=True, ctcss=False)
    assert pump(app, win, lambda: a.tone_mode.currentText() == "Tone")
    # memories: read all, edit a cell, write
    mt = win.memories
    mt.read(mt.ids)
    assert pump(app, win, lambda: not win.worker.pending and mt.data.get("000") is not None, 15)
    row = mt.row_of("040")
    mt.table.item(row, 2).setText("146.52")
    mt.table.item(row, 1).setText("  SIMPLX")         # leading spaces are kept
    assert "040" in mt.dirty
    mt.write_changes()
    assert pump(app, win, lambda: not win.worker.pending and "040" not in mt.dirty)
    assert win.radio.read_memory("040").name == "  SIMPLX"
    # settings were read at connect
    assert pump(app, win, lambda: win.settings_tab.rows["MYC"].widget.text() == "NOCALL")
    st = win.settings_tab
    st.rows["MYC"].widget.setText("VE7ABC")
    st.rows["MYC"].changed()
    st.write_changes()
    assert pump(app, win, lambda: not win.worker.pending and not st.dirty)
    assert win.radio.emulator.settings["MYC"] == "VE7ABC"
    # PTT press/release goes TX then RX
    win.key_tx(True)
    assert pump(app, win, lambda: win.radio.emulator.tx)
    win.key_tx(False)
    assert pump(app, win, lambda: not win.radio.emulator.tx)
    win.close()


def test_gui_panels_are_docks(app, tmp_path):
    store = QtCore.QSettings(str(tmp_path / "layout.ini"), QtCore.QSettings.IniFormat)
    win = MainWindow("emulator", store)
    win.show()
    app.processEvents()
    d = win.docks
    assert set(d) == {"Radio", "Memories", "Settings", "Scan", "Console"}
    # by default the console is beside the panels, not a tab among them
    assert win.dockWidgetArea(d["Console"]) != win.dockWidgetArea(d["Radio"])
    assert d["Memories"] in win.tabifiedDockWidgets(d["Radio"])
    # float the console; the layout is remembered across a restart
    d["Console"].setFloating(True)
    win.close()
    win2 = MainWindow("emulator", store)
    assert win2.docks["Console"].isFloating()
    win2.default_layout()
    assert not win2.docks["Console"].isFloating()
    win2.close()


def test_gui_band_scan_and_click_to_tune(app):
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    sc = win.scan_tab
    sc.band.setCurrentIndex(0)
    sc.start_mhz.setText("146.40")
    sc.stop_mhz.setText("146.60")
    sc.step.setCurrentText("20")
    sc.dwell.setValue(0)
    sc.on_signal.setCurrentIndex(0)                   # don't stop on signals
    win.radio.recall_memory(0, "001")                 # band A in MR: the sweep must put it back
    sc.start()
    a = win.radio_tab.panels[0]
    assert pump(app, win, lambda: a.display.text() == "146.46000")   # follows the sweep
    assert pump(app, win, lambda: not sc.running, 15)
    v = sc.view
    assert v.peaks[v.freqs.index(146_520_000)] == 5  # the emulator's carrier
    assert win.radio.get_band_mode(0) == 2           # restored after the sweep
    # click the peak: band A goes to VFO on that frequency
    x = v._plot_rect()[0] + (v.freqs.index(146_520_000) + 0.5) * v._plot_rect()[2] / len(v.freqs)
    assert v.freqs[v.index_at(x)] == 146_520_000
    v.clicked.emit(v.freqs.index(146_520_000))
    assert pump(app, win, lambda: not win.worker.pending and win.radio.get_vfo(0).freq == 146_520_000)
    assert win.radio.get_band_mode(0) == 0
    # a click during a running sweep stops it and leaves the band there
    sc.repeat.setChecked(True)
    sc.start()
    assert pump(app, win, lambda: sc.index > 3)
    v.clicked.emit(v.freqs.index(146_440_000))
    assert pump(app, win, lambda: not sc.running and not win.worker.pending, 10)
    assert win.radio.get_vfo(0).freq == 146_440_000
    win.close()


def test_gui_about(app):
    from d7ctl.version import build_info
    win = MainWindow("emulator")
    text = win.about_text()
    i = build_info()
    assert f"d7ctl {i['version']}" in text and i["commit"] in text and i["date"] in text
    assert ("uncommitted changes" in text) is i["dirty"]
    win.close()


def test_gui_1750_button_follows_the_radio(app):
    for port, enabled in (("emulator:D7", False), ("emulator", True)):   # TH-D7A, TH-D7G
        win = MainWindow(port)
        win.show()
        win.toggle_connection()
        assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
        assert win.radio_tab.t1750.isEnabled() is enabled
        win.close()


def test_gui_memory_name_editor_stops_at_eight(app):
    from d7ctl import protocol as P
    from d7ctl.gui import MEM_NAME_COLUMN, limit_to_memory_name
    QtTest = pytest.importorskip("PySide6.QtTest")
    editor = limit_to_memory_name(QtWidgets.QLineEdit())
    editor.show()
    QtTest.QTest.keyClicks(editor, "REPEATER1,2")
    assert editor.text() == "REPEATER" and len(editor.text()) == P.NAME_LEN
    editor.clear()
    QtTest.QTest.keyClicks(editor, "A,B")
    assert editor.text() == "AB"                     # no commas
    # plain ASCII only (keyClicks cannot type é, so ask the validator)
    Valid = pytest.importorskip("PySide6.QtGui").QValidator.State
    assert editor.validator().validate("Café", 4)[0] == Valid.Invalid
    assert editor.validator().validate("Caf ~;", 6)[0] == Valid.Acceptable
    # the Memories table's Name column edits with such an editor
    win = MainWindow("emulator")
    t = win.memories.table
    e = t.itemDelegateForColumn(MEM_NAME_COLUMN).createEditor(
        t.viewport(), None, t.model().index(0, MEM_NAME_COLUMN))
    assert e.maxLength() == P.NAME_LEN
    win.close()


def test_gui_store_dialog_shows_used_memories(app):
    from d7ctl.gui import MEMORY_LIST_ROWS, StoreDialog
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    dlg = StoreDialog(win, 0)
    ok = dlg.buttons.button(QtWidgets.QDialogButtonBox.Ok)
    # nothing read yet: 000 is read on its own when picked, and is in use
    assert dlg.memid == "000"
    assert pump(app, win, lambda: "in use" in dlg.state.text())
    assert "W1AW RPT" in dlg.combo.itemText(0) and ok.text() == "Overwrite"
    assert "000" in win.memories.data                 # the Memories tab learnt it too
    # Next empty skips the used 001 and lands on the first empty one
    dlg.next_empty()
    assert pump(app, win, lambda: not dlg.searching and not win.worker.pending)
    assert dlg.memid not in ("000", "001") and win.radio.read_memory(dlg.memid) is None
    assert dlg.state.text() == "empty" and ok.text() == "Store" and ok.isEnabled()
    assert dlg.combo.maxVisibleItems() == MEMORY_LIST_ROWS
    dlg.close()
    win.close()


def test_gui_band_memory_list_and_display_show_names(app):
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    a = win.radio_tab.panels[0]
    assert a.mem_combo.itemText(0) == "000 ?"          # not read yet
    win.memories.read(["000", "001"])
    assert pump(app, win, lambda: not win.worker.pending and "W1AW RPT" in a.mem_combo.itemText(0))
    from d7ctl.gui import UNREAD_ROLE
    assert a.mem_combo.itemData(0, UNREAD_ROLE) is False and a.mem_combo.itemData(5, UNREAD_ROLE) is True
    assert "2M CALL" in win.radio_tab.panels[1].mem_combo.itemText(1)
    # recall from the list: the display line names the memory (read from the radio)
    a.mem_combo.setCurrentIndex(a.mem_combo.findData("001"))
    a._recall()
    assert pump(app, win, lambda: not win.worker.pending and "MR 001 2M CALL" in a.subline.text())
    win.close()


def test_gui_renaming_the_shown_memory_updates_the_display(app):
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    a = win.radio_tab.panels[0]
    a.mem_combo.setCurrentIndex(a.mem_combo.findData("001"))
    a._recall()
    assert pump(app, win, lambda: not win.worker.pending and "MR 001 2M CALL" in a.subline.text())
    # rename it in the Memories table and write
    mt = win.memories
    mt.read(["001"])
    assert pump(app, win, lambda: not win.worker.pending and "001" in mt.data)
    mt.table.item(mt.row_of("001"), 1).setText("SIMPLEX")
    mt.write_changes()
    assert pump(app, win, lambda: not win.worker.pending and "MR 001 SIMPLEX" in a.subline.text())
    # rename it from the Console, trailing spaces and all
    win.console.entry.setText("MNA 0,001,CALL  ")
    win.console.send()
    assert pump(app, win, lambda: not win.worker.pending and "MR 001 CALL" in a.subline.text(), 3)
    assert win.radio.read_memory_name("001") == "CALL  "
    win.close()


def test_memory_label_is_name_or_frequency():
    from d7ctl import protocol as P
    from d7ctl.gui import memory_label
    data = {"000": P.Channel(freq=146_940_000, step=3, name="W1AW RPT"),
            "005": P.Channel(freq=146_520_000, step=3), "004": None}
    assert memory_label("000", data) == "000 W1AW RPT"
    assert memory_label("005", data) == "005 146.520"
    assert memory_label("004", data) == "004"
    assert memory_label("030", data) == "030 ?"


def test_gui_memory_dropdown_fits_a_full_name(app):
    win = MainWindow("emulator")
    win.resize(700, 600)                               # a narrow window squeezes the panels
    win.show()
    app.processEvents()
    c = win.radio_tab.panels[0].mem_combo
    full = c.fontMetrics().horizontalAdvance("000 WWWWWWWW")
    assert c.width() >= full and c.view().minimumWidth() >= full
    win.close()


def test_gui_follows_a_name_entered_on_the_keys(app):
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    mt = win.memories
    mt.read(["001"])
    assert pump(app, win, lambda: not win.worker.pending and "001" in mt.data)
    win.radio.emulator.key_name("001", "BA2uA")         # [F], [9] on the radio
    assert pump(app, win, lambda: mt.table.item(mt.row_of("001"), 1).text() == "BA2uA")
    assert mt.data["001"].name == "BA2uA"
    win.radio.emulator.key_name("002", "UNREAD")        # a memory the GUI has not read
    assert pump(app, win, lambda: not win.worker.pending and mt.data.get("002") is not None
                and mt.table.item(mt.row_of("002"), 1).text() == "UNREAD")
    win.close()


def test_console_counts_trailing_spaces(app):
    win = MainWindow("emulator")
    win.console.add("<", "MNA 0,199,AB  ")
    win.console.add("<", "MNA 0,198,AB")
    lines = win.console.log.toPlainText().splitlines()
    assert lines[-2].endswith("MNA 0,199,AB     [2 trailing spaces]")
    assert lines[-1].endswith("MNA 0,198,AB")
    win.close()


def test_gui_scan_holds_on_a_signal(app):
    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    sc = win.scan_tab
    sc.band.setCurrentIndex(0)
    sc.start_mhz.setText("146.40")
    sc.stop_mhz.setText("146.60")
    sc.step.setCurrentText("20")
    sc.dwell.setValue(0)
    sc.hold.setValue(30)
    sc.start()
    assert pump(app, win, lambda: sc.hold_timer.isActive(), 10)   # stopped on the carrier
    held = sc.index
    assert sc.view.freqs[held - 1] == 146_520_000 and "holding" in sc.info.text()
    time.sleep(0.3)
    app.processEvents()
    assert sc.index == held and win.radio.get_vfo(0).freq == 146_520_000
    sc.toggle()                                                    # Stop ends the hold at once
    assert pump(app, win, lambda: not sc.running and not win.worker.pending, 5)
    assert not sc.hold_timer.isActive()
    win.close()


def _scan_window(app, store=None):
    win = MainWindow("emulator", store)
    win.show()
    win.toggle_connection()
    assert pump(app, win, lambda: win.radio is not None and not win.worker.pending)
    sc = win.scan_tab
    sc.band.setCurrentIndex(0)
    sc.dwell.setValue(0)
    return win, sc


def _sweep_146(sc):
    sc.start_mhz.setText("146.40")
    sc.stop_mhz.setText("146.60")
    sc.step.setCurrentText("20")


def test_gui_scan_threshold_skips_weaker_signals(app):
    win, sc = _scan_window(app)
    _sweep_146(sc)
    sc.threshold.setValue(5)
    sc.hold.setValue(30)
    win.radio.emulator.carriers = [(146_520_000, 4, 5_000)]      # S4: below the threshold
    sc.start()
    assert pump(app, win, lambda: not sc.running, 10)
    assert not sc.holding and sc.view.peaks[sc.view.freqs.index(146_520_000)] == 4
    win.close()


def test_gui_scan_holds_until_the_signal_drops(app):
    win, sc = _scan_window(app)
    _sweep_146(sc)
    sc.on_signal.setCurrentIndex(2)                               # until it drops, then 0 s
    sc.hold.setValue(0)
    emu = win.radio.emulator
    emu.carriers = [(146_520_000, 3, 5_000)]
    sc.start()
    assert pump(app, win, lambda: sc.holding, 10)
    held = sc.index
    time.sleep(1.0)                                               # still there: keeps holding
    app.processEvents()
    assert pump(app, win, lambda: sc.holding and sc.index == held, 1)
    emu.carriers = []                                             # the signal drops
    assert pump(app, win, lambda: not sc.running, 10)
    assert sc.index == len(sc.view.freqs)                         # went on to the end
    win.close()


def test_gui_memory_scan_of_a_band_and_click_to_recall(app):
    win, sc = _scan_window(app)
    sc.mode.setCurrentIndex(1)
    sc.mem_list.setCurrentIndex(sc.mem_list.findData("band:1"))   # VHF, 136-174 MHz
    sc.on_signal.setCurrentIndex(0)
    sc.start()                                                    # reads the memories first
    assert pump(app, win, lambda: not sc.running and sc.view.labels, 20)
    assert sc.view.labels == ["000", "001", "003", "010", "021"]  # no UHF, no airband
    assert sc.view.peaks[sc.view.labels.index("001")] == 5        # the emulator's 146.52 carrier
    assert win.radio.get_band_mode(0) == 0                        # put back as it was (VFO)
    sc.view.clicked.emit(sc.view.labels.index("003"))
    assert pump(app, win, lambda: not win.worker.pending and win.radio.get_band_mode(0) == 2)
    assert win.radio.values("MC", 0)[1] == "003"
    win.close()


def test_gui_memory_scan_lists_are_kept(app, tmp_path):
    from d7ctl.gui import MemoryListDialog
    store = QtCore.QSettings(str(tmp_path / "d7.ini"), QtCore.QSettings.IniFormat)
    win, sc = _scan_window(app, store)
    win.memories.read(["000", "001", "002"])
    assert pump(app, win, lambda: not win.worker.pending and "002" in win.memories.data)
    dlg = MemoryListDialog(win, "test", ["002"])
    assert [dlg.list.item(i).data(QtCore.Qt.UserRole) for i in range(dlg.list.count())] == \
           ["000", "001", "002"]
    dlg.list.item(0).setCheckState(QtCore.Qt.Checked)
    assert dlg.ids() == ["000", "002"]
    sc.lists["repeaters"] = dlg.ids()
    sc._save_lists()
    sc._fill_mem_lists("list:repeaters")
    sc.mode.setCurrentIndex(1)
    sc.on_signal.setCurrentIndex(0)
    sc.start()
    assert pump(app, win, lambda: not sc.running and sc.view.labels == ["000", "002"], 10)
    win.close()
    win2 = MainWindow("emulator", store)                          # the list is remembered
    assert win2.scan_tab.lists == {"repeaters": ["000", "002"]}
    assert win2.scan_tab.mem_list.findData("list:repeaters") >= 0
    win2.close()


def test_band_panel_memory_list_leaves_out_empty_memories(app):
    win, _sc = _scan_window(app)
    win.memories.read(["000", "004"])
    assert pump(app, win, lambda: not win.worker.pending and "004" in win.memories.data)
    c = win.radio_tab.panels[0].mem_combo
    i_used, i_empty, i_unread = (c.findData(m) for m in ("000", "004", "005"))
    assert c.view().isRowHidden(i_empty) and not c.model().item(i_empty).isEnabled()
    assert not c.view().isRowHidden(i_used) and not c.view().isRowHidden(i_unread)
    win.close()


def test_memory_scan_axis_shows_as_many_names_as_fit(app):
    from d7ctl.gui import SpectrumView
    v = SpectrumView()
    v.resize(800, 300)
    v.set_points([146_000_000] * 5, [f"{i:03d}" for i in range(5)], ["W1AW RPT"] * 5)
    indices, upright, margin = v.label_layout()
    assert indices == list(range(5)) and not upright and margin == v.MARGIN_B
    v.set_points([146_000_000] * 50, [f"{i:03d}" for i in range(50)], ["W1AW RPT"] * 50)
    indices, upright, margin = v.label_layout()
    assert upright and indices == list(range(50))          # 15 px a bar: one name each
    assert margin > v.MARGIN_B                               # room for upright names
    v.set_points([146_000_000] * 200, [f"{i:03d}" for i in range(200)], ["CH"] * 200)
    indices, upright, _m = v.label_layout()
    assert upright and 1 < indices[1] and len(indices) > 40  # every k-th, still many
    v.grab()                                                 # paints without error


def test_scan_cursor_marks_the_point_being_measured(app):
    # the last point too: it was marked only once measured, and the scan then ended
    win, sc = _scan_window(app)
    _sweep_146(sc)
    sc.on_signal.setCurrentIndex(0)
    seen = []
    set_cursor = sc.view.set_cursor
    sc.view.set_cursor = lambda i: (seen.append((i, sc.view.levels[i])), set_cursor(i))
    sc.start()
    assert pump(app, win, lambda: not sc.running, 10)
    n = len(sc.view.freqs)
    assert [i for i, _l in seen] == list(range(n))
    assert all(level is None for _i, level in seen)        # marked before it was read
    win.close()
