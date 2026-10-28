"""Regenerate the README screenshots in docs/ from the GUI on the emulator.

Runs offscreen, so no display or radio is needed:
    .venv/bin/python tools/screenshots.py [output directory, default docs]
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from d7ctl.gui import MainWindow  # noqa: E402

SETTLE_S = 0.6                # let the meters poll and the docks repaint before a grab


def main(out: str = "docs"):
    os.makedirs(out, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")

    def pump(cond, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def settle():
        pump(lambda: False, SETTLE_S)

    win = MainWindow("emulator")
    win.show()
    win.toggle_connection()
    pump(lambda: win.radio is not None and not win.worker.pending)
    mt = win.memories
    mt.read(mt.ids)
    pump(lambda: not win.worker.pending and mt.data.get("000") is not None)
    settle()
    win.docks["Radio"].raise_()
    settle()
    win.grab().save(os.path.join(out, "radio.png"))

    # a band sweep over the emulator's 2 m carriers
    sc = win.scan_tab
    sc.band.setCurrentIndex(0)
    sc.start_mhz.setText("146.00")
    sc.stop_mhz.setText("147.00")
    sc.step.setCurrentText("12.5")
    sc.dwell.setValue(0)
    sc.on_signal.setCurrentIndex(0)                 # don't stop on signals
    win.docks["Scan"].raise_()
    sc.start()
    pump(lambda: not sc.running, 60)
    settle()
    win.grab().save(os.path.join(out, "scan.png"))
    win.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
