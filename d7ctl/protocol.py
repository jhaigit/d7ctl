"""TH-D7 serial protocol: value tables, channel records and their wire format.

Everything here is pure (no I/O) so it can be tested without a radio.
The command set is documented in PROTOCOL.md together with its sources.

Wire format in brief: ASCII, one command per line terminated by CR, the
mnemonic followed by one space and comma-separated arguments
("SQ 0,03"). The radio answers every command with one CR-terminated
line: the same mnemonic with the current value, "?" when it did not
understand the command, or "N" when the command is not possible now
(e.g. reading an empty memory).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional

BAUDRATE = 9600            # the D7's PC port is fixed at 9600 8N1, no handshake
EOL = b"\r"                # both directions
ENCODING = "latin-1"       # memory names are 8-bit; latin-1 round-trips every byte
ERROR_REPLIES = ("?", "N")  # "?" = not understood, "N" = not available now

BANDS = ("A", "B")         # band index on the wire: 0 = A, 1 = B

# Tuning steps, kHz. Index (written in hex) is used by ST, BUF, MR/MW, CR/CW.
STEPS_KHZ = (5.0, 6.25, 10.0, 12.5, 15.0, 20.0, 25.0, 30.0, 50.0, 100.0)

# Sub-audible tone table. The wire code is 1-based; code 02 (69.3 Hz) is in
# the table but the D7 does not offer it, so it is listed as None.
TONES_HZ = (
    None,  # code 00 does not exist
    67.0, None, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9,
    186.2, 192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
)
DEFAULT_TONE_CODE = 9      # 88.5 Hz, the radio's factory default

# Repeater shift (SFT, BUF, MR). 3 exists on the European TH-D7E only.
SHIFTS = {0: "simplex", 1: "+", 2: "-", 3: "-7.6M"}
SHIFT_SYMBOLS = {0: "", 1: "+", 2: "-", 3: "-7.6"}

MODES = {0: "FM", 1: "AM"}           # MD; AM is receive-only (air band)
POWERS = {0: "High", 1: "Low", 2: "EL"}   # PC: 5 W / 0.5 W / 50 mW (manual)
BAND_MODES = {0: "VFO", 2: "MR", 3: "CALL"}   # VMC; 1 is unused on the D7

SQUELCH_MAX = 5            # SQ levels 0 (open) .. 5
SMETER_MAX = 5             # SM reports 0 .. 5 bars

MEMORY_COUNT = 200         # memories 000-199
NAME_LEN = 8               # memory names, MNA
# Program-scan edge memories, in the radio's own order.
PSCAN_IDS = tuple(f"{edge}{n}" for n in range(10) for edge in "LU")
CALL_IDS = ("CALL0", "CALL1")        # CR/CW index 0 = VHF call, 1 = UHF call
CALL_LABELS = {"CALL0": "Call VHF", "CALL1": "Call UHF"}


class ProtocolError(ValueError):
    """A reply that does not have the expected shape."""


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------

def band_index(band) -> int:
    """'A'/'B'/0/1 -> 0/1."""
    if isinstance(band, int) and band in (0, 1):
        return band
    text = str(band).strip().upper()
    if text in ("A", "0"):
        return 0
    if text in ("B", "1"):
        return 1
    raise ValueError(f"band must be A or B, not {band!r}")


def tone_code(hz: float) -> int:
    """Nearest-exact lookup of a tone frequency; raises if the D7 lacks it."""
    for code, value in enumerate(TONES_HZ):
        if value is not None and abs(value - float(hz)) < 0.05:
            return code
    raise ValueError(f"{hz} Hz is not a TH-D7 tone frequency")


def tone_hz(code: int) -> Optional[float]:
    if 0 <= code < len(TONES_HZ):
        return TONES_HZ[code]
    return None


def valid_tones():
    return [t for t in TONES_HZ if t is not None]


def step_index(khz: float) -> int:
    for i, value in enumerate(STEPS_KHZ):
        if abs(value - float(khz)) < 0.001:
            return i
    raise ValueError(f"{khz} kHz is not a TH-D7 step; choose from {STEPS_KHZ}")


def memory_ids():
    """Every addressable memory: 000-199, program-scan edges, call channels."""
    return [f"{n:03d}" for n in range(MEMORY_COUNT)] + list(PSCAN_IDS) + list(CALL_IDS)


def normalise_memid(memid) -> str:
    """Accept 5, '5', '005', 'l3', 'call0', 'callv', 'callu' -> canonical id."""
    if isinstance(memid, int):
        text = str(memid)
    else:
        text = str(memid).strip().upper()
    if text.isdigit():
        n = int(text)
        if not 0 <= n < MEMORY_COUNT:
            raise ValueError(f"memory number {n} is outside 0-{MEMORY_COUNT - 1}")
        return f"{n:03d}"
    if text in PSCAN_IDS:
        return text
    aliases = {"CALL0": "CALL0", "CALLV": "CALL0", "CALLVHF": "CALL0",
               "CALL1": "CALL1", "CALLU": "CALL1", "CALLUHF": "CALL1"}
    if text.replace(" ", "") in aliases:
        return aliases[text.replace(" ", "")]
    raise ValueError(f"unknown memory {memid!r} (000-199, L0-L9, U0-U9, CALL0, CALL1)")


def is_call(memid: str) -> bool:
    return memid in CALL_IDS


def format_mhz(hz: Optional[int], places: int = 5) -> str:
    if hz is None:
        return ""
    return f"{hz / 1e6:.{places}f}"


def parse_frequency(text, default_unit: str = "MHz") -> int:
    """'145.525', '145.525M', '145525k', '145525000' -> Hz.

    A bare number with a decimal point, or one below 10 000, is read in
    `default_unit`; a bare large integer is read as Hz.
    """
    s = str(text).strip().lower().replace("_", "").replace(" ", "")
    if s.endswith("hz"):
        s = s[:-2]
    units = {"g": 1e9, "m": 1e6, "k": 1e3}
    if s and s[-1] in units:
        return int(round(float(s[:-1]) * units[s[-1]]))
    value = float(s)
    if "." in s or value < 10_000:
        return int(round(value * units[default_unit[0].lower()]))
    return int(value)


# ---------------------------------------------------------------------------
# channel record
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    """One VFO buffer, memory or call channel, in the radio's own terms.

    Fields map one-to-one onto the wire spec
    freq,step,shift,reverse,tone,ctcss,<dcs>,tone_code,<dcs code>,ctcss_code,offset,mode[,lockout]
    plus the name (MNA) and an odd-split transmit frequency (MR/MW split side).
    """
    freq: int = 145_000_000           # Hz
    step: int = 0                     # index into STEPS_KHZ
    shift: int = 0                    # key of SHIFTS
    reverse: bool = False
    tone: bool = False                # transmit sub-audible tone
    ctcss: bool = False               # tone squelch (encode + decode)
    tone_code: int = DEFAULT_TONE_CODE
    ctcss_code: int = DEFAULT_TONE_CODE
    offset: int = 0                   # Hz, magnitude; direction is `shift`
    mode: int = 0                     # key of MODES
    lockout: bool = False             # skipped by memory scan (memories only)
    name: str = ""
    tx_freq: Optional[int] = None     # odd split: transmit frequency, Hz

    # -- presentation -----------------------------------------------------
    @property
    def step_khz(self) -> float:
        return STEPS_KHZ[self.step] if 0 <= self.step < len(STEPS_KHZ) else 0.0

    @property
    def tone_hz(self) -> Optional[float]:
        return tone_hz(self.tone_code)

    @property
    def ctcss_hz(self) -> Optional[float]:
        return tone_hz(self.ctcss_code)

    @property
    def tone_mode(self) -> str:
        """'' / 'Tone' / 'TSQL' (CHIRP's names)."""
        if self.ctcss:
            return "TSQL"
        if self.tone:
            return "Tone"
        return ""

    @property
    def duplex(self) -> str:
        """'' / '+' / '-' / 'split' (CHIRP's names)."""
        if self.tx_freq is not None and self.shift == 0:
            return "split"
        return SHIFT_SYMBOLS.get(self.shift, "")

    def replace(self, **changes) -> "Channel":
        return dataclasses.replace(self, **changes)

    def summary(self) -> str:
        parts = [f"{format_mhz(self.freq)} MHz", MODES.get(self.mode, "?"),
                 f"step {self.step_khz:g}k"]
        if self.duplex == "split":
            parts.append(f"split TX {format_mhz(self.tx_freq)}")
        elif self.shift:
            parts.append(f"shift {SHIFTS[self.shift]} {format_mhz(self.offset, 3)}")
        if self.tone:
            parts.append(f"tone {self.tone_hz}")
        if self.ctcss:
            parts.append(f"ctcss {self.ctcss_hz}")
        if self.reverse:
            parts.append("rev")
        if self.lockout:
            parts.append("lockout")
        if self.name:
            parts.append(f"'{self.name}'")
        return "  ".join(parts)

    # -- wire format ------------------------------------------------------
    def spec_fields(self, with_lockout: bool) -> list:
        fields = [
            f"{self.freq:011d}",
            f"{self.step:X}",
            str(self.shift),
            str(int(self.reverse)),
            str(int(self.tone)),
            str(int(self.ctcss)),
            "",                       # DCS flag: the D7 has no DCS
            f"{self.tone_code:02d}",
            "",                       # DCS code
            f"{self.ctcss_code:02d}",
            f"{self.offset:09d}",
            str(self.mode),
        ]
        if with_lockout:
            fields.append(str(int(self.lockout)))
        return fields

    def spec(self, with_lockout: bool) -> str:
        return ",".join(self.spec_fields(with_lockout))

    @classmethod
    def from_spec_fields(cls, fields) -> "Channel":
        """Decode the 12 (or 13, with lockout) spec fields."""
        if len(fields) < 11:
            raise ProtocolError(f"channel spec too short: {fields!r}")

        def num(i, base=10, default=0):
            if i < len(fields) and fields[i].strip():
                return int(fields[i], base)
            return default

        try:
            return cls(
                freq=num(0),
                step=num(1, 16),
                shift=num(2),
                reverse=bool(num(3)),
                tone=bool(num(4)),
                ctcss=bool(num(5)),
                tone_code=num(7, default=DEFAULT_TONE_CODE),
                ctcss_code=num(9, default=DEFAULT_TONE_CODE),
                offset=num(10),
                mode=num(11),
                lockout=bool(num(12)),
            )
        except ValueError as exc:
            raise ProtocolError(f"bad channel spec {fields!r}: {exc}") from None

    def validate(self) -> None:
        """Raise ValueError for values the radio would refuse."""
        if not 0 <= self.freq < 10**11:
            raise ValueError("frequency out of range")
        if not 0 <= self.step < len(STEPS_KHZ):
            raise ValueError("bad step index")
        if self.shift not in SHIFTS:
            raise ValueError("bad shift")
        for code in (self.tone_code, self.ctcss_code):
            if tone_hz(code) is None:
                raise ValueError(f"tone code {code} is not available on the D7")
        if self.mode not in MODES:
            raise ValueError("bad mode")
        if not 0 <= self.offset < 10**9:
            raise ValueError("offset out of range")
        if len(self.name) > NAME_LEN or "," in self.name:
            raise ValueError(f"name must be at most {NAME_LEN} characters, no commas")


# ---------------------------------------------------------------------------
# reply parsing
# ---------------------------------------------------------------------------

def split_reply(line: str):
    """'SQ 0,03' -> ('SQ', ['0', '03']); 'LK' -> ('LK', [])."""
    mnemonic, _, rest = line.strip().partition(" ")
    return mnemonic, (rest.split(",") if rest else [])


def parse_buf(line: str):
    """'BUF b,<12 spec fields>' -> (band, Channel)."""
    mnemonic, fields = split_reply(line)
    if mnemonic != "BUF" or len(fields) < 12:
        raise ProtocolError(f"unexpected BUF reply {line!r}")
    return int(fields[0]), Channel.from_spec_fields(fields[1:])


def parse_mr(line: str):
    """'MR 0,split,memid,<12 spec fields>,lockout' -> (memid, split, Channel)."""
    mnemonic, fields = split_reply(line)
    if mnemonic != "MR" or len(fields) < 5:
        raise ProtocolError(f"unexpected MR reply {line!r}")
    split = fields[1] == "1"
    memid = fields[2]
    if split:
        # The transmit side carries only frequency and step that matter.
        ch = Channel(freq=int(fields[3]), step=int(fields[4], 16) if fields[4] else 0)
    else:
        ch = Channel.from_spec_fields(fields[3:])
    return memid, split, ch


def parse_cr(line: str):
    """'CR idx,split,<12 spec fields>' -> (call index, split, Channel)."""
    mnemonic, fields = split_reply(line)
    if mnemonic != "CR" or len(fields) < 4:
        raise ProtocolError(f"unexpected CR reply {line!r}")
    split = fields[1] == "1"
    if split:
        ch = Channel(freq=int(fields[2]), step=int(fields[3], 16) if fields[3] else 0)
    else:
        ch = Channel.from_spec_fields(fields[2:])
    return int(fields[0]), split, ch


# ---------------------------------------------------------------------------
# user-facing key=value channel edits (shared by the CLI and import)
# ---------------------------------------------------------------------------

CHANNEL_KEYS = ("freq", "step", "shift", "offset", "tone", "ctcss", "rev",
                "mode", "lockout", "name", "txfreq")

_TRUE = ("1", "on", "yes", "true", "y")
_FALSE = ("0", "off", "no", "false", "n", "none", "")


def parse_bool(text) -> bool:
    s = str(text).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"expected on/off, not {text!r}")


def apply_channel_edits(ch: Channel, edits: dict) -> Channel:
    """Apply user edits like {'freq': '145.525', 'tone': '88.5'} to a channel.

    tone=<Hz> turns the transmit tone on (and tone squelch off);
    ctcss=<Hz> turns tone squelch on (and the plain tone off), as the
    radio's TONE key cycles off -> T -> CT; tone=off / ctcss=off clear them.
    shift is one of simplex, +, -, -7.6M; offset is in MHz.
    txfreq=<MHz> makes an odd-split memory (shift becomes simplex);
    txfreq=off removes it.
    """
    changes = {}
    for key, value in edits.items():
        key = key.lower()
        text = str(value).strip()
        if key == "freq":
            changes["freq"] = parse_frequency(text)
        elif key == "step":
            changes["step"] = step_index(float(text.lower().rstrip("khz")))
        elif key == "shift":
            lookup = {"simplex": 0, "s": 0, "0": 0, "off": 0, "none": 0,
                      "+": 1, "plus": 1, "-": 2, "minus": 2,
                      "-7.6m": 3, "-7.6": 3}
            if text.lower() not in lookup:
                raise ValueError(f"shift must be simplex, + or -, not {text!r}")
            changes["shift"] = lookup[text.lower()]
        elif key == "offset":
            changes["offset"] = parse_frequency(text)
        elif key in ("tone", "ctcss"):
            if text.lower() in _FALSE:
                changes[key] = False
            else:
                code = tone_code(float(text))
                changes[key] = True
                changes[f"{key}_code"] = code
                other = "ctcss" if key == "tone" else "tone"
                changes.setdefault(other, False)
        elif key in ("rev", "reverse"):
            changes["reverse"] = parse_bool(text)
        elif key == "mode":
            inv = {v.lower(): k for k, v in MODES.items()}
            if text.lower() not in inv:
                raise ValueError("mode must be FM or AM")
            changes["mode"] = inv[text.lower()]
        elif key in ("lockout", "skip"):
            changes["lockout"] = parse_bool(text)
        elif key == "name":
            changes["name"] = text
        elif key in ("txfreq", "tx"):
            if text.lower() in _FALSE:
                changes["tx_freq"] = None
            else:
                changes["tx_freq"] = parse_frequency(text)
                changes["shift"] = 0
        else:
            raise ValueError(f"unknown channel field {key!r}; use {', '.join(CHANNEL_KEYS)}")
    out = ch.replace(**changes)
    out.validate()
    return out
