import os
import subprocess
import sys
import time

import pytest

from d7ctl import protocol as P
from d7ctl.radio import CommandRejected, D7Radio
from d7ctl import memfile


@pytest.fixture
def radio():
    r = D7Radio("emulator")
    r.open()
    yield r
    r.close()


def test_identify_and_status(radio):
    assert radio.model == "TH-D7G"
    st = radio.status()
    assert len(st["bands"]) == 2 and st["bands"][0]["vfo"].freq == 144_390_000


def test_vfo_edit(radio):
    ch = radio.update_vfo("B", freq=446_100_000, shift=2, offset=5_000_000, tone=True,
                          tone_code=P.tone_code(100.0))
    assert ch.freq == 446_100_000 and ch.tone_hz == 100.0
    assert radio.get_vfo(1) == ch


def test_vfo_fallback_when_buf_write_refused(radio):
    # A radio that answers '?' to a BUF write gets the piecewise commands instead.
    emu = radio.emulator
    orig = emu._cmd_BUF
    emu._cmd_BUF = lambda args: "?" if len(args) > 1 else orig(args)
    radio.set_control_band(0)
    ch = radio.update_vfo(1, freq=440_025_000, shift=1, offset=5_000_000)
    assert ch.freq == 440_025_000 and ch.shift == 1
    assert radio.get_control_band() == 0          # restored after acting on band B


def test_band_controls(radio):
    radio.set_squelch(0, 4)
    assert radio.get_squelch(0) == 4
    radio.set_power(1, "el")
    assert P.POWERS[radio.get_power(1)] == "EL"
    radio.set_control_band("B")
    assert radio.get_control_band() == 1
    radio.recall_memory("A", 1)
    assert radio.get_band_mode(0) == 2 and radio.get_memory_channel(0) == "001"
    assert radio.get_vfo(0).freq == 146_520_000
    f = radio.get_vfo(1).freq
    radio.step(1, +1)
    assert radio.get_vfo(1).freq == f + 12_500


def test_memories_round_trip(radio):
    ch = P.Channel(freq=145_230_000, step=3, shift=2, offset=600_000, ctcss=True,
                   ctcss_code=P.tone_code(123.0), name="TEST", lockout=True)
    radio.write_memory(150, ch)
    assert radio.read_memory("150") == ch
    split = P.Channel(freq=145_800_000, step=3, tx_freq=437_800_000, name="SAT")
    radio.write_memory("151", split)
    back = radio.read_memory("151")
    assert back.duplex == "split" and back.tx_freq == 437_800_000
    radio.erase_memory(150)
    assert radio.read_memory(150) is None
    radio.write_memory("CALL1", P.Channel(freq=433_500_000, step=3))
    assert radio.read_memory("CALL1").freq == 433_500_000


def test_settings(radio):
    for s in radio.settings():
        if s.key != "MNF":
            radio.read_setting(s)          # every other registered setting answers
    s = next(s for s in radio.settings() if s.key == "APO")
    radio.write_setting(s, "60 min")
    assert radio.read_setting(s) == "60 min"
    s = next(s for s in radio.settings() if s.key == "MNF")
    with pytest.raises(CommandRejected):
        radio.read_setting(s)              # only in MR mode


def test_auto_info_events(radio):
    got = []
    radio.add_event_listener(got.append)
    radio.set_auto_info(True)
    radio.set_control_band(1)
    radio.command("UP")
    time.sleep(0.1)
    assert any(line.startswith("BUF 1,") for line in got)


def test_reply_matching_ignores_other_band_event(radio):
    # With AI on, a "BY 1,..." report while waiting for "BY 0" must not be taken as the reply.
    emu = radio.emulator
    emu.ai = True
    orig = emu._cmd_BY

    def by(args):
        emu._emit("BY 1,1")
        return orig(args)
    emu._cmd_BY = by
    emu.busy = [False, True]
    assert radio.get_busy(0) is False


def test_csv_and_backup(tmp_path, radio):
    mems = radio.read_all_memories()
    path = tmp_path / "m.csv"
    n = memfile.export_csv(str(path), mems)
    assert n == sum(v is not None for v in mems.values())
    back, warnings = memfile.import_csv(str(path))
    assert not warnings
    for memid, ch in back.items():
        orig = mems[memid]
        assert (ch.freq, ch.name, ch.duplex, ch.tx_freq, ch.tone_mode) == \
               (orig.freq, orig.name, orig.duplex, orig.tx_freq, orig.tone_mode)
    bpath = tmp_path / "b.json"
    memfile.write_backup(str(bpath), radio.model, mems, radio.read_all_settings())
    doc = memfile.read_backup(str(bpath))
    assert doc["memories"]["000"] == mems["000"]


def test_real_serial_path_over_pty():
    """The same driver through pyserial, against the emulator on a pseudo-terminal."""
    proc = subprocess.Popen([sys.executable, "-m", "d7ctl.emulator", "--model", "TH-D7"],
                            stdout=subprocess.PIPE, text=True,
                            cwd=os.path.dirname(os.path.dirname(__file__)))
    try:
        line = proc.stdout.readline()
        port = line.split(" on ")[1].split()[0]
        r = D7Radio(port)
        assert r.open() == "TH-D7"
        assert r.read_memory(0).name == "W1AW RPT"
        r.set_frequency(0, 145_000_000)
        assert r.get_vfo(0).freq == 145_000_000
        r.close()
    finally:
        proc.terminate()
        proc.wait(5)
