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
    win.radio.recall_memory(0, "001")                 # band A in MR: the sweep must put it back
    sc.start()
    assert pump(app, win, lambda: not sc.running, 15)
    v = sc.view
    assert v.peaks[v.freqs.index(146_520_000)] == 5  # the emulator's carrier
    assert win.radio.get_band_mode(0) == 2           # restored after the sweep
    # click the peak: band A goes to VFO on that frequency
    x = v._plot_rect()[0] + (v.freqs.index(146_520_000) + 0.5) * v._plot_rect()[2] / len(v.freqs)
    assert v.freqs[v.index_at(x)] == 146_520_000
    v.clicked.emit(146_520_000)
    assert pump(app, win, lambda: not win.worker.pending and win.radio.get_vfo(0).freq == 146_520_000)
    assert win.radio.get_band_mode(0) == 0
    # a click during a running sweep stops it and leaves the band there
    sc.repeat.setChecked(True)
    sc.start()
    assert pump(app, win, lambda: sc.index > 3)
    v.clicked.emit(146_440_000)
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
