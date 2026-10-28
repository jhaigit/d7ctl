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


def test_settings_with_trailing_fields(radio):
    # the TH-D7 answers "APO 1,0" and "BEL 0,0," (2026-10-27)
    s = next(s for s in radio.settings() if s.key == "APO")
    assert s.decode("1,0") == "30 min"
    sent = []
    radio.add_traffic_listener(lambda d, line: d == ">" and sent.append(line))
    radio.write_setting(s, "Off")
    assert radio.command("APO") == "APO 0"             # off: no second field, as on the radio
    radio.read_setting_raw(s)
    radio.write_setting(s, "60 min")
    assert radio.command("APO") == "APO 2,0"           # on: the second field comes back
    assert [l for l in sent if l.startswith("APO ")] == ["APO 0", "APO 2,0"]
    s = next(s for s in radio.settings() if s.key == "BEL 0")
    assert radio.read_setting_raw(s) == "0,"
    radio.write_setting(s, True)
    assert radio.command("BEL 0") == "BEL 0,1,"


def test_hidden_band_buffer(radio):
    radio.command("DL 0")                              # single band: B is hidden
    assert radio.command("BUF 1", check=False) == "N"
    st = radio.status()
    assert st["dual"] is False and st["bands"][1] == {"band": "B", "hidden": True}


def test_power_codes(radio):
    # a TH-D7A showing L answered "PC 0,2" (2026-10-27): 0 High, 2 Low, 3 EL
    radio.set_power(0, "low")
    assert radio.command("PC 0") == "PC 0,2"
    radio.set_power(0, "el")
    assert radio.command("PC 0") == "PC 0,3"
    s = next(s for s in radio.settings() if s.key == "PC 0")
    assert s.decode("2") == "Low" and s.encode("EL") == "3"


def test_tnc_in_packet_mode(monkeypatch):
    # the TH-D7A's TNC in PACKET mode answers "cmd:ID" then "?EH" (2026-10-27)
    import d7ctl.emulator as emu
    from d7ctl.radio import TncPacketMode

    class PacketEmulator(emu.Emulator):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.packet_mode = True

    monkeypatch.setattr(emu, "Emulator", PacketEmulator)
    r = D7Radio("emulator", timeout=0.3)
    with pytest.raises(TncPacketMode, match="PACKET"):
        r.open()
    assert r.emulator.handle("TC 1") == "TC 1" and not r.emulator.packet_mode


def test_menu_values_read_off_a_th_d7a():
    # display vs reply on a TH-D7A (2026-10-27)
    from d7ctl.settings import SETTINGS
    s = {x.key: x for x in SETTINGS}
    assert [s["TXI"].decode(str(i)) for i in (0, 3, 4, 7)] == ["0.5 min", "3 min", "5 min", "30 min"]
    assert [s["SV"].decode(str(i)) for i in (0, 1, 5, 9)] == ["Off", "0.2 s", "1.0 s", "5 s"]
    assert [s["APO"].decode(r) for r in ("0", "1,0", "2,0")] == ["Off", "30 min", "60 min"]
    r = D7Radio("emulator:D7")
    r.open()
    assert r.command("TH", check=False) == "N"
    r.close()


def test_sweep(radio):
    radio.recall_memory(0, "001")                      # band A in MR mode
    before = radio.get_vfo(0)
    saved = radio.sweep_begin(0)
    assert radio.get_band_mode(0) == 0
    freqs = P.sweep_frequencies(146_400_000, 146_600_000, 20)
    levels = {hz: radio.sweep_point(saved, hz, 20, 0) for hz in freqs}
    assert max(levels, key=levels.get) == 146_520_000 and levels[146_520_000] == 5
    radio.sweep_end(saved)
    assert radio.get_band_mode(0) == 2 and radio.get_vfo(0).freq == before.freq
    saved = radio.sweep_begin(0)
    radio.sweep_end(saved, tune_hz=146_520_000)          # a click on the graph
    assert radio.get_band_mode(0) == 0 and radio.get_vfo(0).freq == 146_520_000


def test_sweep_frequencies():
    assert P.sweep_frequencies(146_401_000, 146_460_000, 20) == [146_420_000, 146_440_000, 146_460_000]
    assert P.sweep_frequencies(145_000_000, 145_012_500, 6.25)[-1] == 145_012_500


def test_memory_names_keep_spaces_and_symbols(radio):
    # the TH-D7A stores and returns "  Testx" and "Test;x" unchanged (2026-10-28)
    for name in ("  Testx", "Test;x", "AB  ", " A B C "):
        radio.write_memory(199, P.Channel(freq=146_520_000, step=3, name=name))
        assert radio.read_memory(199).name == name


def test_band_status_names_the_memory(radio):
    radio.recall_memory(0, "000")
    st = radio.band_status(0)
    assert st["memory"] == "000" and st["name"] == "W1AW RPT"
    radio.set_band_mode(0, "VFO")
    assert radio.band_status(0)["name"] == ""


def test_memory_names_are_plain_ascii(radio):
    # the TH-D7A stores a space for é (2026-10-28); the driver refuses such names first
    with pytest.raises(ValueError):
        radio.write_memory(199, P.Channel(freq=146_520_000, step=3, name="Café"))
    for bad in ("A,B", "Café"):
        with pytest.raises(ValueError):
            P.Channel(freq=146_520_000, step=3, name=bad).validate()
    P.Channel(freq=146_520_000, step=3, name=" !+-~;Az").validate()
    radio.write_memory(199, P.Channel(freq=146_520_000, step=3))
    assert radio.command("MNA 0,199,Café") == "MNA 0,199,Caf "
