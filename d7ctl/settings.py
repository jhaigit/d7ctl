"""Registry of the TH-D7's menu settings and how each is read and written.

A setting's key is the command as sent to read it: a bare mnemonic
("LK") or a mnemonic with an index ("SQ 0", "MP 2"). Reading sends the
key and the reply is "<mnemonic> [<index>,]<value>". Writing sends
"<mnemonic> [<index>,]<value>" and the radio echoes it.

Values are held in two forms: `raw` is the string on the wire, `value`
is what a person reads (True/False, a choice label, an int, text).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .protocol import POWERS, TONES_HZ, parse_bool

# Radio models as reported by "ID": TH-D7A and TH-D7E answer "TH-D7";
# the TH-D7A(G) / TH-D7E(G) answer "TH-D7G".
D7 = "TH-D7"
D7G = "TH-D7G"
ALL = ()

# Basic APRS station icons (ICO g,c with g = 0); any other icon is
# written as "1,<table><symbol>", e.g. "1,/>" (car), and is kept as text.
ICONS = (
    ("Kenwood", "0,0"), ("Runner", "0,1"), ("House", "0,2"), ("Tent", "0,3"),
    ("Boat", "0,4"), ("SSTV", "0,5"), ("Plane", "0,6"), ("Speedboat", "0,7"),
    ("Car", "0,8"), ("Bicycle", "0,9"), ("Triangle (DF)", "0,A"), ("Jeep", "0,B"),
    ("Recreational vehicle", "0,C"), ("Truck", "0,D"), ("Van", "0,E"),
)

SSTV_COLOURS = ("Black", "Blue", "Red", "Magenta", "Green", "Cyan", "Yellow", "White")
SSTV_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !?-/"
DTMF_CHARSET = "0123456789ABCD*# "

TIME_ZONES = tuple(
    "UTC" if m == 0 else f"UTC {'+' if m > 0 else '-'} {abs(m) // 60}:{abs(m) % 60:02d}"
    for m in range(-12 * 60, 12 * 60 + 1, 30))


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: str                      # bool | choice | map | int | text | position | range | dtmf
    choices: tuple = ()            # choice: labels, wire value = index; map: (label, raw)
    digits: int = 1                # zero-padded width of a choice/int on the wire
    minimum: int = 0
    maximum: int = 0
    max_len: int = 0
    charset: Optional[str] = None
    models: tuple = ALL            # empty = every model
    restore: bool = True           # safe to write back when restoring a backup
    tail: str = ""                 # extra fields the radio sends after the value (",0" in "APO 1,0")
    tail_when_off: bool = True     # False: no tail with value 0 ("APO 0" but "APO 1,0")
    note: str = ""

    # -- wire ------------------------------------------------------------
    @property
    def mnemonic(self) -> str:
        return self.key.split(" ", 1)[0]

    @property
    def index(self) -> Optional[str]:
        parts = self.key.split(" ", 1)
        return parts[1] if len(parts) == 2 else None

    def read_command(self) -> str:
        return self.key

    def write_command(self, raw: str) -> str:
        if self.index is not None:
            return f"{self.mnemonic} {self.index},{raw}"
        return f"{self.mnemonic} {raw}"

    def raw_from_reply(self, reply: str) -> str:
        """Strip the mnemonic and index from a reply, leaving the value."""
        mnemonic, _, rest = reply.partition(" ")
        if mnemonic != self.mnemonic:
            raise ValueError(f"reply {reply!r} is not for {self.mnemonic}")
        if self.index is not None:
            prefix = self.index + ","
            if not rest.startswith(prefix):
                raise ValueError(f"reply {reply!r} is not for {self.key}")
            rest = rest[len(prefix):]
        return rest

    def applies_to(self, model: Optional[str]) -> bool:
        return not self.models or model is None or model in self.models

    # -- conversions -------------------------------------------------------
    def tail_for(self, value: str, last: str = "") -> str:
        """Trailing fields to send after `value`: those last read, else the registered ones."""
        if not self.tail_when_off and value.strip("0") == "":
            return ""
        return last or self.tail

    def value_field(self, raw: str) -> str:
        """The value of a bool/choice/int, without any trailing fields."""
        return raw.split(",", 1)[0] if self.kind in ("bool", "choice", "int") else raw

    def decode(self, raw: str):
        k = self.kind
        raw = self.value_field(raw)
        if k == "bool":
            return raw.strip() == "1"
        if k == "choice":
            i = int(raw)
            return self.choices[i] if 0 <= i < len(self.choices) else f"#{i}"
        if k == "map":
            for label, value in self.choices:
                if value == raw:
                    return label
            return raw
        if k == "int":
            return int(raw)
        if k == "position":
            return decode_position(raw)
        if k == "range":
            lo, _, hi = raw.partition(",")
            return f"{int(lo)}-{int(hi)}"
        if k == "dtmf":
            return raw.replace("E", "*").replace("F", "#")
        return raw

    def encode(self, value) -> str:
        k = self.kind
        if k == "bool":
            if isinstance(value, str):
                value = parse_bool(value)
            return "1" if value else "0"
        if k == "choice":
            if isinstance(value, int):
                i = value
            else:
                text = str(value).strip()
                lowered = [c.lower() for c in self.choices]
                if text.lower() in lowered:
                    i = lowered.index(text.lower())
                elif text.isdigit():
                    i = int(text)
                else:
                    raise ValueError(f"{self.key}: choose one of {', '.join(self.choices)}")
            if not 0 <= i < len(self.choices):
                raise ValueError(f"{self.key}: index {i} out of range")
            return f"{i:0{self.digits}d}"
        if k == "map":
            text = str(value).strip()
            for label, raw in self.choices:
                if text.lower() in (label.lower(), raw.lower()):
                    return raw
            if self.key == "ICO" and len(text) >= 3 and text[1] == ",":
                return text            # any APRS symbol as "g,xx"
            raise ValueError(f"{self.key}: choose one of {', '.join(l for l, _ in self.choices)}")
        if k == "int":
            n = int(value)
            if not self.minimum <= n <= self.maximum:
                raise ValueError(f"{self.key}: {n} outside {self.minimum}-{self.maximum}")
            return f"{n:0{self.digits}d}"
        if k == "position":
            return encode_position(str(value))
        if k == "range":
            text = str(value).replace(",", "-")
            lo, _, hi = text.partition("-")
            lo, hi = int(lo), int(hi)
            if not self.minimum <= lo <= hi <= self.maximum:
                raise ValueError(f"{self.key}: limits must lie in {self.minimum}-{self.maximum} MHz, low <= high")
            return f"{lo:05d},{hi:05d}"
        text = str(value)
        if self.max_len and len(text) > self.max_len:
            raise ValueError(f"{self.key}: at most {self.max_len} characters")
        if "," in text and self.key not in ("PP", "AMGG"):
            raise ValueError(f"{self.key}: commas are not allowed")
        if self.charset:
            bad = sorted({c for c in text.upper() if c not in self.charset})
            if bad:
                raise ValueError(f"{self.key}: characters not allowed: {''.join(bad)}")
            if self.kind != "dtmf":
                text = text.upper()
        if k == "dtmf":
            return text.replace("*", "E").replace("#", "F")
        return text

    def describe_values(self) -> str:
        k = self.kind
        if k == "bool":
            return "on|off"
        if k == "choice":
            return "|".join(self.choices)
        if k == "map":
            return "|".join(l for l, _ in self.choices) + (" or g,xx" if self.key == "ICO" else "")
        if k == "int":
            return f"{self.minimum}-{self.maximum}"
        if k == "position":
            return "DD MM.MM N|S DDD MM.MM E|W"
        if k == "range":
            return f"LOW-HIGH MHz within {self.minimum}-{self.maximum}"
        return f"text, {self.max_len} chars max" if self.max_len else "text"


# ---------------------------------------------------------------------------
# positions: "ddmmhhssdddmmhhww" -> "37 00.15 N 121 34.96 W"
# (degrees, minutes, hundredths of minutes, 00/01 north/south ...)
# ---------------------------------------------------------------------------

def decode_position(raw: str) -> str:
    raw = raw.strip()
    if len(raw) < 17 or not raw.isdigit():
        return raw
    lat_d, lat_m, south = int(raw[0:2]), int(raw[2:6]) / 100, int(raw[6:8])
    lon_d, lon_m, west = int(raw[8:11]), int(raw[11:15]) / 100, int(raw[15:17])
    return (f"{lat_d:02d} {lat_m:05.2f} {'S' if south else 'N'} "
            f"{lon_d:03d} {lon_m:05.2f} {'W' if west else 'E'}")


def encode_position(text: str) -> str:
    parts = text.replace("°", " ").replace("'", " ").split()
    if len(parts) != 6:
        raise ValueError("position: give 'DD MM.MM N|S DDD MM.MM E|W'")
    lat_d, lat_m, ns, lon_d, lon_m, ew = parts
    lat_d, lon_d = int(lat_d), int(lon_d)
    lat_h, lon_h = round(float(lat_m) * 100), round(float(lon_m) * 100)
    if not (0 <= lat_d <= 90 and 0 <= lon_d <= 180 and 0 <= lat_h < 6000 and 0 <= lon_h < 6000):
        raise ValueError("position out of range")
    if ns.upper() not in "NS" or ew.upper() not in "EW":
        raise ValueError("position: hemisphere letters are N/S and E/W")
    return (f"{lat_d:02d}{lat_h:04d}{int(ns.upper() == 'S'):02d}"
            f"{lon_d:03d}{lon_h:04d}{int(ew.upper() == 'W'):02d}")


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def _b(key, label, group, **kw):
    return Setting(key, label, group, "bool", **kw)


def _c(key, label, group, choices, **kw):
    kw.setdefault("digits", len(str(len(choices) - 1)))
    return Setting(key, label, group, "choice", choices=tuple(choices), **kw)


def _t(key, label, group, max_len, **kw):
    return Setting(key, label, group, "text", max_len=max_len, **kw)


POWER_CODES = tuple((label, str(code)) for code, label in POWERS.items())
SQUELCH = ("0 (open)", "1", "2", "3", "4", "5")

SETTINGS = [
    # --- operation ------------------------------------------------------
    _c("BC", "Control band", "Operation", ("A", "B")),
    _b("DL", "Dual band display", "Operation"),
    _c("BAL", "Volume balance A:B", "Operation", ("4:0", "3:1", "2:2", "1:3", "0:4")),
    _b("LK", "Key lock", "Operation"),
    _b("LMP", "Lamp", "Operation"),
    _b("MON", "Monitor (squelch open)", "Operation"),
    _b("CH", "Channel display mode", "Operation"),
    _c("MNF", "Memory display", "Operation", ("Name", "Frequency"),
       note="readable only while the control band is in MR mode"),
    _c("SQ 0", "Squelch band A", "Operation", SQUELCH, digits=2),
    _c("SQ 1", "Squelch band B", "Operation", SQUELCH, digits=2),
    Setting("PC 0", "TX power band A", "Operation", "map", choices=POWER_CODES),
    Setting("PC 1", "TX power band B", "Operation", "map", choices=POWER_CODES),
    _b("ASC 0", "Auto simplex check band A", "Operation",
       note="needs band A shown (dual band on or A is control band)"),
    _b("ASC 1", "Auto simplex check band B", "Operation",
       note="needs band B shown (dual band on or B is control band)"),
    # The TH-D7 answers "BEL 0,0," - an empty field after the value (2026-10-27)
    _b("BEL 0", "Tone alert band A", "Operation", tail=",", note="needs band A shown"),
    _b("BEL 1", "Tone alert band B", "Operation", tail=",", note="needs band B shown"),
    _b("TNC", "TNC (APRS / packet)", "Operation", restore=False,
       note="in PACKET mode the PC port belongs to the TNC and control is lost"),
    _b("BCN", "APRS beacon", "Operation"),
    # --- radio (menu 1) -------------------------------------------------
    _t("MES", "Power-on message", "Radio", 8),
    Setting("CNT", "LCD contrast", "Radio", "int", minimum=1, maximum=16, digits=2),
    _c("SV", "Battery saver", "Radio",
       ("Off", "0.2 s", "0.4 s", "0.6 s", "0.8 s", "1.0 s", "2 s", "3 s", "4 s", "5 s")),
    # The TH-D7A answers "APO 0" when off but "APO 1,0", "APO 2,0" at 30/60 min;
    # the second field's meaning is unknown (2026-10-27)
    _c("APO", "Auto power off", "Radio", ("Off", "30 min", "60 min"), tail=",0",
       tail_when_off=False),
    _b("ARO", "Auto repeater offset", "Radio"),
    _c("SCR", "Scan resume", "Radio", ("Time", "Carrier", "Seek")),
    _c("BEP", "Key beep", "Radio", ("Off", "Key", "Key+Data", "All")),
    _b("ELK", "Tuning knob disabled (lock)", "Radio"),
    _b("TXS", "TX inhibit", "Radio"),
    _b("AIP", "Advanced intercept point (VHF)", "Radio"),
    # 1750 Hz is a European repeater feature: the TH-D7A has no such menu item and
    # answers "N" (2026-10-27). The A and E both report ID TH-D7, so TH stays listed.
    _b("TH", "TX hold, 1750 Hz", "Radio", note="1750 Hz models (TH-D7E) only"),
    _c("CKEY", "CALL key function", "Radio", ("Call", "1750 Hz"), models=(D7G,)),
    _b("NSFT", "Beat shift", "Radio",
       note="moves the CPU clock to clear a birdie on the receive frequency"),
    # --- DTMF -----------------------------------------------------------
    _b("TSP", "DTMF fast/slow (on = slow)", "DTMF"),
    _b("TXH", "DTMF TX hold", "DTMF"),
    _c("PT", "DTMF pause", "DTMF",
       ("100 ms", "200 ms", "500 ms", "750 ms", "1000 ms", "1500 ms", "2000 ms")),
]
for _n in range(10):
    SETTINGS.append(_t(f"DMN {_n:02d}", f"DTMF memory {_n} name", "DTMF", 8))
    SETTINGS.append(Setting(f"DM {_n:02d}", f"DTMF memory {_n} digits", "DTMF", "dtmf",
                            max_len=16, charset=DTMF_CHARSET))

SETTINGS += [
    # --- TNC --------------------------------------------------------------
    _c("DTB", "Data band", "TNC", ("A", "B"), models=(D7,)),
    _c("DTB", "Data band", "TNC", ("A", "B", "A:TX / B:RX", "A:RX / B:TX"), models=(D7G,)),
    _c("DS", "DCD sense", "TNC", ("Data band", "Both bands"), models=(D7,)),
    _c("DS", "DCD sense", "TNC", ("Data band", "Both bands", "Ignore DCD"), models=(D7G,)),
    _c("PKSA", "APRS packet speed", "TNC", ("1200", "9600"), models=(D7G,)),
    _c("DTBA", "APRS data band", "TNC",
       ("A", "B", "A:TX / B:RX", "A:RX / B:TX"), models=(D7G,)),
    # --- APRS -------------------------------------------------------------
    _t("MYC", "My call sign", "APRS", 9),
    _c("GU", "GPS receiver", "APRS", ("Not used", "NMEA"), models=(D7,)),
    _c("GU", "GPS receiver", "APRS", ("Not used", "NMEA", "NMEA 9600"), models=(D7G,)),
    _c("WAY", "Waypoint output", "APRS",
       ("Off", "6 digit NMEA", "7 digit NMEA", "8 digit NMEA", "9 digit NMEA",
        "6 digit Magellan", "DGPS"), models=(D7G,)),
    Setting("MP", "My position", "APRS", "position", models=(D7,)),
    Setting("MP 1", "My position 1", "APRS", "position", models=(D7G,)),
    Setting("MP 2", "My position 2", "APRS", "position", models=(D7G,)),
    Setting("MP 3", "My position 3", "APRS", "position", models=(D7G,)),
    _c("PAMB", "Position ambiguity", "APRS",
       ("Off", "1 digit", "2 digits", "3 digits", "4 digits"), models=(D7G,)),
    _c("POSC", "Position comment", "APRS",
       ("Off duty", "Enroute", "In service", "Returning", "Committed", "Special",
        "Priority", "Custom 0", "Custom 1", "Custom 2", "Custom 3", "Custom 4",
        "Custom 5", "Custom 6", "Emergency")),
    Setting("ARL", "Reception distance limit (0 = off)", "APRS", "int",
            minimum=0, maximum=2500, digits=4, note="10-mile/km steps"),
    Setting("ICO", "Station icon", "APRS", "map", choices=ICONS,
            note="other symbols: 1,<table><symbol>, e.g. 1,/>"),
    _t("STAT", "Status text", "APRS", 20, models=(D7,)),
    _t("STAT 1", "Status text 1", "APRS", 32, models=(D7G,)),
    _t("STAT 2", "Status text 2", "APRS", 32, models=(D7G,)),
    _t("STAT 3", "Status text 3", "APRS", 32, models=(D7G,)),
    _c("STXR", "Status text rate", "APRS",
       ("Off", "1/1", "1/2", "1/3", "1/4", "1/5", "1/6", "1/7", "1/8"), models=(D7G,)),
    _t("PP", "Packet path", "APRS", 32),
    _c("DTX", "Beacon transmit method", "APRS", ("Manual", "PTT", "Auto")),
    # Read off a TH-D7A, TXI 0-7 (2026-10-27): there is no 4 min, as in the manual
    _c("TXI", "Beacon interval", "APRS",
       ("0.5 min", "1 min", "2 min", "3 min", "5 min", "10 min", "20 min", "30 min")),
    _t("UPR", "Group code (unproto)", "APRS", 9,
       charset="ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"),
    # BEPT and TEMP: the TH-D7A answers "?" (unknown command) to both (2026-10-27)
    _c("BEPT", "APRS beep", "APRS", ("Off", "Mine", "All new", "All"), models=(D7G,)),
    _c("DSPA", "Display area", "APRS", ("Entire display", "One line"), models=(D7G,)),
    _c("KILO", "Distance unit", "APRS", ("Miles", "Kilometres"), models=(D7G,)),
    _c("TEMP", "Temperature unit", "APRS", ("°F", "°C"), models=(D7G,)),
    _b("AMR", "Auto answer reply", "APRS", models=(D7G,)),
    _t("ARLM", "Reply message", "APRS", 45, models=(D7G,)),
    _t("AMGG", "Message groups", "APRS", 45, models=(D7G,),
       charset="ABCDEFGHIJKLMNOPQRSTUVWXYZ*,-0123456789"),
    _c("TZ", "Time zone", "APRS", TIME_ZONES, digits=2, models=(D7G,)),
    Setting("TXD", "Packet TX delay", "APRS", "map", models=(D7G,),
            choices=(("100 ms", "1"), ("200 ms", "2"), ("300 ms", "3"), ("400 ms", "4"),
                     ("500 ms", "5"), ("750 ms", "6"), ("1000 ms", "7"))),
    # --- SSTV (VC-H1) -----------------------------------------------------
    _t("SMY", "SSTV call sign", "SSTV", 8, charset=SSTV_CHARSET),
    _c("MAC", "Call sign colour", "SSTV", SSTV_COLOURS),
    _t("SMSG", "SSTV message", "SSTV", 9, charset=SSTV_CHARSET),
    _c("SMC", "Message colour", "SSTV", SSTV_COLOURS),
    _t("RSV", "RSV report", "SSTV", 10, charset=SSTV_CHARSET),
    _c("RSC", "RSV colour", "SSTV", SSTV_COLOURS),
    _b("VCS", "VC-H1 control", "SSTV"),
    # --- Sky Command --------------------------------------------------------
    _t("SCC", "Commander call sign", "Sky Command", 8),
    _t("SCT", "Transporter call sign", "Sky Command", 8),
    Setting("SKTN", "Sky Command tone", "Sky Command", "map",
            choices=tuple((f"{hz} Hz", f"{code:02d}") for code, hz in
                          enumerate(TONES_HZ) if hz is not None)),
    # --- programmable VFO limits (MHz) ------------------------------------
    Setting("PV 1", "Prog. VFO band A 118 MHz", "Programmable VFO", "range", minimum=118, maximum=135),
    Setting("PV 2", "Prog. VFO band A VHF", "Programmable VFO", "range", minimum=136, maximum=173),
    Setting("PV 3", "Prog. VFO band B VHF", "Programmable VFO", "range", minimum=144, maximum=147),
    Setting("PV 6", "Prog. VFO band B UHF", "Programmable VFO", "range", minimum=400, maximum=479),
]

GROUPS = []
for _s in SETTINGS:
    if _s.group not in GROUPS:
        GROUPS.append(_s.group)


def settings_for(model: Optional[str]):
    """Settings that apply to `model` (all of them when the model is unknown,
    with the D7G variant preferred where the two differ)."""
    out, seen = [], set()
    for s in SETTINGS:
        if model is None:
            if s.models and D7G not in s.models:
                continue
        elif not s.applies_to(model):
            continue
        if s.key in seen:
            continue
        seen.add(s.key)
        out.append(s)
    return out


def find(key: str, model: Optional[str] = None) -> Setting:
    wanted = " ".join(key.upper().split())
    for s in settings_for(model):
        if s.key == wanted:
            return s
    raise KeyError(f"no setting {key!r}")
