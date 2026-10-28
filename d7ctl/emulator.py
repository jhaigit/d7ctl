"""A simulated TH-D7 / TH-D7G for testing without the radio.

It answers the same command set as the real radio (as far as it is
documented in PROTOCOL.md) and keeps state: both bands' buffers,
200 memories plus program-scan and call channels, and every setting in
the registry. With auto-info on it reports changes the way the radio
does, and `activity=True` makes signals come and go on both bands.

    python -m d7ctl.emulator            # serves on a pseudo-terminal, prints its path
    d7ctl -p emulator status            # or use it in-process
"""

from __future__ import annotations

import argparse
import os
import random
import threading
import time

from . import protocol as P
from .protocol import Channel
from .settings import settings_for


class Emulator:
    def __init__(self, model: str = "TH-D7G", activity: bool = False, seed: int = 7):
        self.model = model
        self.lock = threading.RLock()
        self.async_sink = None            # fn(line) for unsolicited output
        self.ai = False
        self.control = 0
        self.dual = True
        self.tx = False
        self.packet_mode = False          # TNC in PACKET mode: it owns the port
        self.vfo = [Channel(freq=144_390_000, step=3), Channel(freq=446_000_000, step=3)]
        self.band_mode = [0, 0]
        self.mc = ["000", "000"]
        self.squelch = [2, 2]
        self.power = [0, 0]
        self.smeter = [0, 0]
        self.busy = [False, False]
        self.memories: dict = {}          # memid -> Channel (with name, tx_freq)
        self.calls = [Channel(freq=146_520_000, step=3), Channel(freq=446_000_000, step=3)]
        self.settings: dict = {}
        for s in settings_for(model):
            self.settings[s.key] = _default_raw(s)
        self.settings["MYC"] = "NOCALL"
        self.settings["MES"] = "HELLO !!"
        self._seed_memories()
        self.activity = activity
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        if activity:
            threading.Thread(target=self._activity_loop, daemon=True).start()

    def _seed_memories(self):
        demo = [
            ("000", Channel(freq=146_940_000, step=3, shift=2, offset=600_000, tone=True,
                            tone_code=P.tone_code(100.0), name="W1AW RPT")),
            ("001", Channel(freq=146_520_000, step=3, name="2M CALL")),
            ("002", Channel(freq=446_000_000, step=3, name="70CM SMP")),
            ("003", Channel(freq=144_390_000, step=3, name="APRS")),
            ("010", Channel(freq=162_550_000, step=6, name="WX1")),
            ("020", Channel(freq=121_500_000, step=6, mode=1, name="AIR GRD")),
            ("021", Channel(freq=145_800_000, step=3, tx_freq=437_800_000, name="ISS XBND")),
            ("L0", Channel(freq=144_000_000, step=3)),
            ("U0", Channel(freq=148_000_000, step=3)),
        ]
        for memid, ch in demo:
            self.memories[memid] = ch

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    def _emit(self, line: str):
        if self.ai and self.async_sink:
            self.async_sink(line)

    def _buf_line(self, b: int) -> str:
        return f"BUF {b}," + self.vfo[b].spec(with_lockout=False)

    def _activity_loop(self):
        while not self._stop.wait(0.7):
            with self.lock:
                if self.tx:
                    continue
                b = self._rng.randrange(2)
                level = self._rng.choice([0, 0, 0, 1, 2, 3, 4, 5])
                if level != self.smeter[b]:
                    self.smeter[b] = level
                    self._emit(f"SM {b},{level}")
                busy = level > self.squelch[b] - 1 and level > 0
                if busy != self.busy[b]:
                    self.busy[b] = busy
                    self._emit(f"BY {b},{int(busy)}")

    # ------------------------------------------------------------------
    def handle(self, line: str) -> str:
        """One command line in, one reply line out."""
        with self.lock:
            try:
                return self._handle(line.strip())
            except (ValueError, IndexError, KeyError, P.ProtocolError):
                return "?"

    def _handle(self, line: str) -> str:
        if self.packet_mode:
            # TNC: echo after the prompt, refuse; "TC 1" hands the port back
            # (TC 1 is from the Kenwood command lists, not yet seen on the radio)
            if line.replace(" ", "").upper() == "TC1":
                self.packet_mode = False
                return "TC 1"
            return f"cmd:{line}\r?EH"
        if not line:
            return "?"
        mn, args = P.split_reply(line)
        h = getattr(self, f"_cmd_{mn}", None)
        if h is not None:
            return h(args)
        return self._generic(mn, args)

    # -- generic settings ------------------------------------------------
    def _generic(self, mn, args):
        if args and f"{mn} {args[0]}" in self.settings:
            key = f"{mn} {args[0]}"
            if len(args) > 1:
                self.settings[key] = ",".join(args[1:])
            return f"{mn} {args[0]},{self.settings[key]}"
        if mn in self.settings:
            if args:
                self.settings[mn] = ",".join(args)
            return f"{mn} {self.settings[mn]}"
        return "?"

    def _band(self, args, i=0) -> int:
        b = int(args[i])
        if b not in (0, 1):
            raise ValueError
        return b

    # -- identity / global -------------------------------------------------
    def _cmd_ID(self, args):
        return f"ID {self.model}"

    def _cmd_AI(self, args):
        if args:
            self.ai = args[0] == "1"
        return f"AI {int(self.ai)}"

    def _cmd_BC(self, args):
        if args:
            self.control = self._band(args)
            self._emit(f"BC {self.control}")
        return f"BC {self.control}"

    def _cmd_DL(self, args):
        if args:
            self.dual = args[0] == "1"
        return f"DL {int(self.dual)}"

    # -- band buffers ------------------------------------------------------
    def _cmd_BUF(self, args):
        b = self._band(args)
        if not self.dual and b != self.control:
            return "N"                     # a hidden band, as on the TH-D7 (2026-10-27)
        if len(args) > 1:
            ch = Channel.from_spec_fields(args[1:])
            ch.validate()
            self.vfo[b] = ch
            self.band_mode[b] = 0
        return self._buf_line(b)

    def _cmd_FQ(self, args):
        b = self.control
        if args:
            self.vfo[b].freq = int(args[0])
            if len(args) > 1:
                self.vfo[b].step = int(args[1], 16)
            self._emit(self._buf_line(b))
        return f"FQ {self.vfo[b].freq:011d},{self.vfo[b].step:X}"

    def _ctl_field(self, mn, attr, args, fmt=str, parse=int):
        ch = self.vfo[self.control]
        if args:
            setattr(ch, attr, parse(args[0]))
        return f"{mn} {fmt(getattr(ch, attr))}"

    def _cmd_SFT(self, a): return self._ctl_field("SFT", "shift", a)
    def _cmd_OS(self, a): return self._ctl_field("OS", "offset", a, lambda v: f"{v:09d}")
    def _cmd_TO(self, a): return self._ctl_field("TO", "tone", a, lambda v: str(int(v)), lambda s: s == "1")
    def _cmd_CT(self, a): return self._ctl_field("CT", "ctcss", a, lambda v: str(int(v)), lambda s: s == "1")
    def _cmd_TN(self, a): return self._ctl_field("TN", "tone_code", a, lambda v: f"{v:02d}")
    def _cmd_CTN(self, a): return self._ctl_field("CTN", "ctcss_code", a, lambda v: f"{v:02d}")
    def _cmd_REV(self, a): return self._ctl_field("REV", "reverse", a, lambda v: str(int(v)), lambda s: s == "1")
    def _cmd_MD(self, a): return self._ctl_field("MD", "mode", a)
    def _cmd_ST(self, a): return self._ctl_field("ST", "step", a, lambda v: f"{v:X}", lambda s: int(s, 16))

    def _cmd_VMC(self, args):
        b = self._band(args)
        if len(args) > 1:
            m = int(args[1])
            if m not in P.BAND_MODES:
                return "N"
            self.band_mode[b] = m
            if m == 2:
                self._load_memory(b)
            elif m == 3:
                self.vfo[b] = self.calls[0 if self.vfo[b].freq < 300_000_000 else 1].replace()
        return f"VMC {b},{self.band_mode[b]}"

    def _load_memory(self, b):
        ch = self.memories.get(self.mc[b])
        if ch is not None:
            self.vfo[b] = ch.replace(name="", lockout=False)

    def _cmd_MC(self, args):
        b = self._band(args)
        if len(args) > 1:
            if self.band_mode[b] != 2:
                return "N"
            memid = P.normalise_memid(args[1])
            if memid not in self.memories:
                return "N"
            self.mc[b] = memid
            self._load_memory(b)
            self._emit(self._buf_line(b))
        return f"MC {b},{self.mc[b]}"

    def _cmd_SQ(self, args):
        b = self._band(args)
        if len(args) > 1:
            self.squelch[b] = int(args[1], 16)
        return f"SQ {b},{self.squelch[b]:02X}"

    def _cmd_PC(self, args):
        b = self._band(args)
        if len(args) > 1:
            if int(args[1]) not in P.POWERS:
                return "N"
            self.power[b] = int(args[1])
        return f"PC {b},{self.power[b]}"

    def _cmd_SM(self, args):
        b = self._band(args)
        return f"SM {b},{self.smeter[b]}"

    def _cmd_BY(self, args):
        b = self._band(args)
        return f"BY {b},{int(self.busy[b])}"

    # -- actions -------------------------------------------------------------
    def _cmd_TX(self, args):
        if self.settings.get("TXS") == "1":
            return "N"
        self.tx = True
        return "TX 0"

    def _cmd_TT(self, args):
        self.tx = True
        return "TT 0"

    def _cmd_RX(self, args):
        self.tx = False
        return "RX 0"

    def _cmd_UP(self, args):
        return self._knob(+1, "UP")

    def _cmd_DW(self, args):
        return self._knob(-1, "DW")

    def _knob(self, d, mn):
        b = self.control
        if self.band_mode[b] == 2:
            ids = sorted(k for k in self.memories if k.isdigit())
            if ids:
                i = ids.index(self.mc[b]) if self.mc[b] in ids else 0
                self.mc[b] = ids[(i + d) % len(ids)]
                self._load_memory(b)
        else:
            ch = self.vfo[b]
            ch.freq += d * int(ch.step_khz * 1000)
        self._emit(self._buf_line(b))
        return mn

    def _cmd_SC(self, args):
        return f"SC {args[0] if args else '0'}"

    def _cmd_MSH(self, args):
        b = self.control
        if self.band_mode[b] != 2:
            return "N"
        self.band_mode[b] = 0
        return "MSH"

    # -- memories -------------------------------------------------------------
    def _cmd_MR(self, args):
        split, memid = args[1] == "1", P.normalise_memid(args[2])
        ch = self.memories.get(memid)
        if ch is None:
            return "N"
        if split:
            tx = ch.tx_freq if ch.tx_freq is not None else ch.freq
            return f"MR 0,1,{memid},{tx:011d},{ch.step:X},0,0,0,0,,{ch.tone_code:02d},,{ch.ctcss_code:02d},000000000,0,0"
        return f"MR 0,0,{memid}," + ch.spec(with_lockout=True)

    def _cmd_MW(self, args):
        split, memid = args[1] == "1", P.normalise_memid(args[2])
        spec = args[3:]
        if split:
            ch = self.memories.get(memid)
            if ch is None:
                return "N"
            ch.tx_freq = int(spec[0])
            ch.shift = 0
        elif not spec:
            self.memories.pop(memid, None)
        else:
            ch = Channel.from_spec_fields(spec)
            ch.validate()
            old = self.memories.get(memid)
            ch.name = old.name if old else ""
            self.memories[memid] = ch
        return "MW " + ",".join(args[:3])

    def _cmd_MNA(self, args):
        memid = P.normalise_memid(args[1])
        ch = self.memories.get(memid)
        if ch is None:
            return "N"
        if len(args) > 2:
            name = ",".join(args[2:])
            if len(name) > P.NAME_LEN:
                return "N"
            ch.name = name
        return f"MNA 0,{memid},{ch.name}"

    def _cmd_CR(self, args):
        i, split = int(args[0]), args[1] == "1"
        ch = self.calls[i]
        if split:
            tx = ch.tx_freq if ch.tx_freq is not None else ch.freq
            return f"CR {i},1,{tx:011d},{ch.step:X},0,0,0,0,,{ch.tone_code:02d},,{ch.ctcss_code:02d},000000000,0"
        return f"CR {i},0," + ch.spec(with_lockout=False)

    def _cmd_CW(self, args):
        i, split = int(args[0]), args[1] == "1"
        if split:
            self.calls[i].tx_freq = int(args[2])
            self.calls[i].shift = 0
        else:
            ch = Channel.from_spec_fields(args[2:])
            ch.validate()
            self.calls[i] = ch
        return f"CW {i},{int(split)}"

    # MNF needs MR on the control band, as on the radio
    def _cmd_MNF(self, args):
        if self.band_mode[self.control] != 2:
            return "N"
        return self._generic("MNF", args)

    # APO: "APO 0" when off, "APO n,0" when on, as on the TH-D7A
    def _cmd_APO(self, args):
        if args:
            s = next(s for s in settings_for(self.model) if s.key == "APO")
            self.settings["APO"] = args[0] + s.tail_for(args[0])
        return f"APO {self.settings['APO']}"

    # TH (1750 Hz hold): the TH-D7A answers N; emulated as an A, not an E
    def _cmd_TH(self, args):
        return "N" if self.model == "TH-D7" else self._generic("TH", args)

    # ASC/BEL on a hidden band are refused, as on the radio
    def _cmd_ASC(self, args):
        return self._shown_band_only("ASC", args)

    def _cmd_BEL(self, args):
        return self._shown_band_only("BEL", args)

    def _shown_band_only(self, mn, args):
        b = self._band(args)
        if not self.dual and b != self.control:
            return "N"
        return self._generic(mn, args)


def _default_raw(s) -> str:
    if s.kind == "bool":
        return "0" + s.tail_for("0")
    if s.kind in ("choice", "int"):
        value = f"{s.minimum:0{s.digits}d}" if s.kind == "int" else "0" * s.digits
        return value + s.tail_for(value)
    if s.kind == "map":
        return s.choices[0][1]
    if s.kind == "position":
        return "00000000000000000"
    if s.kind == "range":
        return f"{s.minimum:05d},{s.maximum:05d}"
    return ""


class FakeSerial:
    """Just enough of serial.Serial for D7Radio to talk to an Emulator."""

    def __init__(self, emulator: Emulator, timeout: float = 0.05):
        self.emulator = emulator
        self.timeout = timeout
        self._in = b""
        self._out = bytearray()
        self._cv = threading.Condition()
        self.is_open = True
        emulator.async_sink = self._push

    def _push(self, line: str):
        with self._cv:
            self._out += line.encode(P.ENCODING) + P.EOL
            self._cv.notify_all()

    def write(self, data: bytes) -> int:
        self._in += data
        while P.EOL in self._in:
            raw, self._in = self._in.split(P.EOL, 1)
            reply = self.emulator.handle(raw.decode(P.ENCODING))
            self._push(reply)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._cv:
            if not self._out:
                self._cv.wait(self.timeout)
            chunk = bytes(self._out[:size])
            del self._out[:size]
            return chunk

    def close(self):
        self.is_open = False
        self.emulator.stop()


def serve_pty(emulator: Emulator) -> None:
    """Serve the emulator on a pseudo-terminal until interrupted."""
    import pty
    import tty
    master, slave = pty.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)
    lock = threading.Lock()

    def sink(line):
        with lock:
            os.write(master, line.encode(P.ENCODING) + P.EOL)

    emulator.async_sink = sink
    print(f"{emulator.model} emulator on {path}   (d7ctl -p {path} status)", flush=True)
    buf = b""
    try:
        while True:
            data = os.read(master, 256)
            buf += data
            while P.EOL in buf:
                raw, buf = buf.split(P.EOL, 1)
                line = raw.decode(P.ENCODING).strip("\n ")
                if line or raw == b"":
                    sink(emulator.handle(line))
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Simulated Kenwood TH-D7 on a pseudo-terminal")
    ap.add_argument("--model", choices=("TH-D7", "TH-D7G"), default="TH-D7G")
    ap.add_argument("--activity", action="store_true",
                    help="make signals come and go (S-meter / busy auto-info)")
    args = ap.parse_args(argv)
    serve_pty(Emulator(model=args.model, activity=args.activity))


if __name__ == "__main__":
    main()
