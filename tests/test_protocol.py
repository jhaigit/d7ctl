import pytest

from d7ctl import protocol as P
from d7ctl import settings as S


def test_mr_example_from_reference():
    # Example reply quoted in the TA1DX TH-D7 command list.
    memid, split, ch = P.parse_mr("MR 0,0,001,00440000000,6,0,0,0,0,,09,,09,000000000,0,0")
    assert (memid, split) == ("001", False)
    assert ch.freq == 440_000_000 and ch.step_khz == 25.0
    assert ch.tone_hz == 88.5 and not ch.tone and not ch.lockout


def test_cw_example_round_trip():
    # "CW 0,0,00144410000,0,0,0,0,0,,09,,09,000600000,0" from the same list
    fields = "00144410000,0,0,0,0,0,,09,,09,000600000,0".split(",")
    ch = P.Channel.from_spec_fields(fields)
    assert ch.spec(with_lockout=False) == ",".join(fields)


def test_buf_parse_and_encode():
    line = "BUF 1,00446000000,3,1,0,1,0,,13,,09,005000000,0"
    band, ch = P.parse_buf(line)
    assert band == 1 and ch.shift == 1 and ch.offset == 5_000_000 and ch.tone_hz == 100.0
    assert "BUF 1," + ch.spec(False) == line


def test_tone_table():
    assert len(P.valid_tones()) == 38
    assert P.tone_code(67.0) == 1 and P.tone_code(71.9) == 3 and P.tone_code(250.3) == 39
    with pytest.raises(ValueError):
        P.tone_code(69.3)          # code 02 exists in the table, not on the D7


@pytest.mark.parametrize("text,hz", [("145.525", 145_525_000), ("145525k", 145_525_000),
                                     ("145525000", 145_525_000), ("0.6", 600_000), ("446M", 446_000_000)])
def test_parse_frequency(text, hz):
    assert P.parse_frequency(text) == hz


def test_channel_edits():
    ch = P.apply_channel_edits(P.Channel(), {"freq": "146.94", "shift": "-", "offset": "0.6",
                                             "tone": "100", "name": "RPT"})
    assert ch.freq == 146_940_000 and ch.shift == 2 and ch.tone and not ch.ctcss
    ch = P.apply_channel_edits(ch, {"ctcss": "100"})
    assert ch.ctcss and not ch.tone
    ch = P.apply_channel_edits(ch, {"txfreq": "147.45"})
    assert ch.duplex == "split" and ch.shift == 0
    with pytest.raises(ValueError):                   # cross-band: the TH-D7A refuses it
        P.apply_channel_edits(ch, {"txfreq": "437.8"})
    with pytest.raises(ValueError):
        P.apply_channel_edits(ch, {"name": "TOOLONGNAME"})


def test_memids():
    assert P.normalise_memid(5) == "005" and P.normalise_memid("l3") == "L3"
    assert P.normalise_memid("callu") == "CALL1"
    assert len(P.memory_ids()) == 200 + 20 + 2
    with pytest.raises(ValueError):
        P.normalise_memid(200)


def test_settings_codecs():
    sq = S.find("SQ 0")
    assert sq.write_command(sq.encode(3)) == "SQ 0,03"
    assert sq.decode(sq.raw_from_reply("SQ 0,05")) == "5"
    lk = S.find("LK")
    assert lk.write_command(lk.encode("on")) == "LK 1"
    mp = S.find("MP 1", "TH-D7G")
    raw = "37001500121349601"           # TA1DX example
    assert mp.encode(mp.decode(raw)) == raw
    dm = S.find("DM 03")
    assert dm.encode("*123#") == "E123F" and dm.decode("E123F") == "*123#"
    pv = S.find("PV 2")
    assert pv.encode("136-173") == "00136,00173"
    with pytest.raises(ValueError):
        pv.encode("100-173")


def test_model_variants():
    keys_a = {s.key for s in S.settings_for("TH-D7")}
    keys_g = {s.key for s in S.settings_for("TH-D7G")}
    assert "MP" in keys_a and "MP 1" not in keys_a
    assert {"MP 1", "STAT 3", "TZ"} <= keys_g and "MP" not in keys_g
    assert S.find("DS", "TH-D7G").choices[-1] == "Ignore DCD"
