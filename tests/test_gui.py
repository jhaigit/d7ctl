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
    mt.table.item(row, 1).setText("SIMPLEX")
    assert "040" in mt.dirty
    mt.write_changes()
    assert pump(app, win, lambda: not win.worker.pending and "040" not in mt.dirty)
    assert win.radio.read_memory("040").name == "SIMPLEX"
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
