"""Memory and backup files.

CSV uses CHIRP's column layout, so a file exported here opens in CHIRP
and a CHIRP export of another radio can be loaded into the D7 (DCS and
other features the D7 lacks are ignored). Program-scan and call memories
are written with their D7 names (L0..U9, CALL0, CALL1) in Location.

The JSON backup holds every memory and every setting's raw value.
"""

from __future__ import annotations

import csv
import datetime
import json

from . import protocol as P
from .protocol import Channel

CHIRP_COLUMNS = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone",
                 "rToneFreq", "cToneFreq", "DtcsCode", "DtcsPolarity", "RxDtcsCode",
                 "CrossMode", "Mode", "TStep", "Skip", "Power", "Comment",
                 "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE"]


def _location(memid: str) -> str:
    return str(int(memid)) if memid.isdigit() else memid


def channel_to_row(memid: str, ch: Channel) -> dict:
    if ch.duplex == "split":
        duplex, offset = "split", ch.tx_freq / 1e6
    else:
        duplex = {0: "", 1: "+", 2: "-", 3: "-"}[ch.shift]
        offset = (7_600_000 if ch.shift == 3 else ch.offset) / 1e6
    return {
        "Location": _location(memid), "Name": ch.name, "Frequency": f"{ch.freq / 1e6:.6f}",
        "Duplex": duplex, "Offset": f"{offset:.6f}", "Tone": ch.tone_mode,
        "rToneFreq": f"{ch.tone_hz or 88.5:.1f}", "cToneFreq": f"{ch.ctcss_hz or 88.5:.1f}",
        "DtcsCode": "023", "DtcsPolarity": "NN", "RxDtcsCode": "023",
        "CrossMode": "Tone->Tone", "Mode": P.MODES[ch.mode], "TStep": f"{ch.step_khz:.2f}",
        "Skip": "S" if ch.lockout else "", "Power": "", "Comment": "",
        "URCALL": "", "RPT1CALL": "", "RPT2CALL": "", "DVCODE": "",
    }


def _nearest_step(khz: float) -> int:
    return min(range(len(P.STEPS_KHZ)), key=lambda i: abs(P.STEPS_KHZ[i] - khz))


def _tone_code_or_default(text) -> int:
    try:
        return P.tone_code(float(text))
    except (TypeError, ValueError):
        return P.DEFAULT_TONE_CODE


def row_to_channel(row: dict):
    """-> (memid, Channel, warnings)."""
    warnings = []
    memid = P.normalise_memid(row["Location"].strip())
    freq = P.parse_frequency(row["Frequency"])
    duplex = (row.get("Duplex") or "").strip()
    offset_hz = int(round(float(row.get("Offset") or 0) * 1e6))
    ch = Channel(freq=freq, name=(row.get("Name") or "")[:P.NAME_LEN])
    if row.get("Name") and len(row["Name"]) > P.NAME_LEN:
        warnings.append(f"{memid}: name cut to {P.NAME_LEN} characters")
    if duplex == "+":
        ch.shift, ch.offset = 1, offset_hz
    elif duplex == "-":
        ch.shift, ch.offset = 2, offset_hz
    elif duplex == "split":
        ch.tx_freq = offset_hz
    elif duplex == "off":
        warnings.append(f"{memid}: TX-off memories are not supported; stored as simplex")
    tone = (row.get("Tone") or "").strip()
    ch.tone_code = _tone_code_or_default(row.get("rToneFreq"))
    ch.ctcss_code = _tone_code_or_default(row.get("cToneFreq"))
    if tone == "Tone":
        ch.tone = True
    elif tone == "TSQL":
        ch.ctcss = True
    elif tone:
        warnings.append(f"{memid}: tone mode {tone} not supported by the D7; ignored")
    mode = (row.get("Mode") or "FM").strip().upper()
    if mode in ("FM", "NFM"):
        ch.mode = 0
    elif mode == "AM":
        ch.mode = 1
    else:
        warnings.append(f"{memid}: mode {mode} not supported; using FM")
    try:
        ch.step = _nearest_step(float(row.get("TStep") or 5))
    except ValueError:
        ch.step = 0
    ch.lockout = (row.get("Skip") or "").strip() == "S"
    if P.is_call(memid):
        ch.lockout = False
    ch.validate()
    return memid, ch, warnings


def export_csv(path: str, memories: dict) -> int:
    """memories: {memid: Channel or None}. Empty ones are skipped."""
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CHIRP_COLUMNS)
        w.writeheader()
        for memid in P.memory_ids():
            ch = memories.get(memid)
            if ch is not None:
                w.writerow(channel_to_row(memid, ch))
                n += 1
    return n


def import_csv(path: str):
    """-> ({memid: Channel}, warnings)."""
    out, warnings = {}, []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            if not (row.get("Location") or "").strip():
                continue
            try:
                memid, ch, w = row_to_channel(row)
            except (ValueError, KeyError) as exc:
                warnings.append(f"line {lineno}: skipped ({exc})")
                continue
            out[memid] = ch
            warnings += w
    return out, warnings


# ---------------------------------------------------------------------------
# JSON backup
# ---------------------------------------------------------------------------

def channel_to_dict(ch: Channel) -> dict:
    return {"freq": ch.freq, "step": ch.step, "shift": ch.shift, "reverse": ch.reverse,
            "tone": ch.tone, "ctcss": ch.ctcss, "tone_code": ch.tone_code,
            "ctcss_code": ch.ctcss_code, "offset": ch.offset, "mode": ch.mode,
            "lockout": ch.lockout, "name": ch.name, "tx_freq": ch.tx_freq}


def channel_from_dict(d: dict) -> Channel:
    return Channel(**{k: d[k] for k in channel_to_dict(Channel()) if k in d})


def write_backup(path: str, model: str, memories: dict, settings: dict) -> None:
    doc = {
        "format": "d7ctl-backup-1",
        "model": model,
        "saved": datetime.datetime.now().isoformat(timespec="seconds"),
        "memories": {k: channel_to_dict(v) for k, v in memories.items() if v is not None},
        "empty_memories": sorted(k for k, v in memories.items() if v is None),
        "settings": {k: v for k, v in settings.items() if v is not None},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)


def read_backup(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("format") != "d7ctl-backup-1":
        raise ValueError(f"{path} is not a d7ctl backup")
    doc["memories"] = {k: channel_from_dict(v) for k, v in doc["memories"].items()}
    return doc
