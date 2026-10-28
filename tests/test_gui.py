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
    assert set(d) == {"Radio", "Memories", "Settings", "Console"}
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
