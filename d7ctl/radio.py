"""Talking to a TH-D7 over its serial port.

`D7Radio` owns the port and a reader thread. Each command waits for the
one reply line that answers it; any other line (the radio's auto-info
reports when AI is on, e.g. "BUF 0,..." after the knob turns, "BY 1,1"
when band B opens) is passed to the event listeners instead.

Port names are anything pyserial accepts ("/dev/ttyUSB0", "COM3",
"socket://host:port", "rfc2217://...") plus "emulator", "emulator:D7"
and "emulator:D7G" for the built-in simulated radio.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, Optional

from . import protocol as P
from .protocol import Channel
from .settings import Setting, settings_for

DEFAULT_TIMEOUT = 1.0      # s; the D7 answers within ~50 ms, MW can take longer
WRITE_SETTLE = 0.05        # s between MW and MNA: the radio drops a name sent too soon


class RadioError(Exception):
    pass


class RadioTimeout(RadioError):
    pass


class TncPacketMode(RadioError):
    """The TNC, not the radio, owns the port: it is in PACKET mode."""


class CommandRejected(RadioError):
    """The radio replied '?' (not understood) or 'N' (not possible now)."""

    def __init__(self, command: str, reply: str):
        self.command, self.reply = command, reply
        why = "not understood" if reply == "?" else "not available now"
        super().__init__(f"radio refused {command!r}: {reply} ({why})")


class _Pending:
    def __init__(self, line: str):
        self.line = line
        self.mnemonic, self.args = P.split_reply(line)
        self.reply: Optional[str] = None
        self.done = threading.Event()

    def matches(self, reply: str) -> bool:
        if reply in P.ERROR_REPLIES:
            return True
        mnemonic, args = P.split_reply(reply)
        if mnemonic != self.mnemonic:
            return False
        # "BY 0" is answered by "BY 0,1"; an auto-info "BY 1,0" arriving
        # meanwhile is for the other band and must not be taken as the reply.
        if self.args and args and self.mnemonic not in _UNINDEXED_REPLIES:
            return args[0] == self.args[0]
        return True


# Commands whose first argument is a value, not an index the reply repeats.
_UNINDEXED_REPLIES = {"FQ", "AI", "BC", "LK", "LMP", "DL", "BAL", "MON", "SC",
                      "MD", "SFT", "OS", "ST", "TO", "CT", "TN", "CTN", "REV",
                      "TX", "RX", "TT", "MSH", "UP", "DW", "MIN", "TNC", "BCN"}


class D7Radio:
    def __init__(self, port: str, baudrate: int = P.BAUDRATE,
                 timeout: float = DEFAULT_TIMEOUT, rtscts: bool = False):
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.rtscts = rtscts
        self.model: Optional[str] = None
        self._ser = None
        self._lock = threading.RLock()          # one command at a time
        self._pending: Optional[_Pending] = None
        self._pending_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._event_listeners: list = []
        self._traffic_listeners: list = []
        self._widths: dict = {}                 # setting key -> width seen on the wire
        self._tails: dict = {}                  # setting key -> fields after the value, as last read
        self._tnc_seen = False                  # a TNC prompt arrived (see TNC_PROMPTS)
        self.emulator = None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def open(self, auto_info: bool = False) -> str:
        """Open the port, identify the radio, set auto-info; returns the model."""
        if self.port_name.lower().startswith("emulator"):
            from .emulator import FakeSerial, Emulator
            _, _, variant = self.port_name.partition(":")
            self.emulator = Emulator(model="TH-D7" if variant.upper() == "D7" else "TH-D7G")
            self._ser = FakeSerial(self.emulator)
        else:
            import serial
            self._ser = serial.serial_for_url(
                self.port_name, baudrate=self.baudrate, bytesize=8, parity="N",
                stopbits=1, timeout=0.05, rtscts=self.rtscts, xonxoff=False)
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="d7-reader", daemon=True)
        self._reader.start()
        # A lone CR makes the radio discard any half-typed command; its "?"
        # reply (if any) is swallowed as an event.
        self._write_raw("")
        time.sleep(0.15)
        try:
            self.model = self.identify()
        except RadioTimeout:
            self.close()
            if self._tnc_seen:
                raise TncPacketMode(
                    "the TNC answered instead of the radio: it is in PACKET mode. Press [TNC] "
                    "on the radio until PACKET leaves the display") from None
            raise
        except RadioError:
            self.close()
            raise
        self.set_auto_info(auto_info)
        return self.model

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=1)
        if self._ser is not None:
            with contextlib.suppress(Exception):
                self._ser.close()
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def __enter__(self):
        if not self.is_open:
            self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # listeners
    # ------------------------------------------------------------------
    def add_event_listener(self, fn: Callable[[str], None]) -> None:
        """fn(line) for every unsolicited line (auto-info). Runs on the reader thread."""
        self._event_listeners.append(fn)

    def add_traffic_listener(self, fn: Callable[[str, str], None]) -> None:
        """fn(direction, line) with direction '>' (to radio) or '<' (from radio)."""
        self._traffic_listeners.append(fn)

    def _notify_traffic(self, direction: str, line: str) -> None:
        for fn in list(self._traffic_listeners):
            with contextlib.suppress(Exception):
                fn(direction, line)

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------
    def _write_raw(self, line: str) -> None:
        if self._ser is None:
            raise RadioError("not connected")
        self._ser.write(line.encode(P.ENCODING, errors="replace") + P.EOL)

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception:           # port unplugged or closed under us
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            if not chunk:
                continue
            buf += chunk
            while P.EOL in buf:
                raw, buf = buf.split(P.EOL, 1)
                # only line ends: spaces belong to the reply (names keep them)
                line = raw.decode(P.ENCODING).strip("\r\n")
                if line:
                    self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        self._notify_traffic("<", line)
        if line.startswith(P.TNC_PROMPTS):
            self._tnc_seen = True
        with self._pending_lock:
            pending = self._pending
            if pending is not None and pending.reply is None and pending.matches(line):
                pending.reply = line
                pending.done.set()
                return
        for fn in list(self._event_listeners):
            with contextlib.suppress(Exception):
                fn(line)

    def command(self, cmd: str, *args, timeout: Optional[float] = None,
                check: bool = True) -> str:
        """Send one command and return the radio's reply line.

        `cmd` may be a whole line ("SQ 0,03") or a mnemonic with `args`
        ("SQ", 0, "03"). With check=True a '?' or 'N' reply raises
        CommandRejected.
        """
        line = cmd if not args else f"{cmd} " + ",".join(str(a) for a in args)
        with self._lock:
            pending = _Pending(line)
            with self._pending_lock:
                self._pending = pending
            try:
                self._notify_traffic(">", line)
                self._write_raw(line)
                if not pending.done.wait(self.timeout if timeout is None else timeout):
                    raise RadioTimeout(f"no reply to {line!r} - is the radio on, "
                                       "at 9600 baud, with the TNC not in PACKET mode?")
            finally:
                with self._pending_lock:
                    self._pending = None
        reply = pending.reply
        if check and reply in P.ERROR_REPLIES:
            raise CommandRejected(line, reply)
        return reply

    def values(self, cmd: str, *args, **kw) -> list:
        """command() and return the reply's comma-separated fields."""
        return P.split_reply(self.command(cmd, *args, **kw))[1]

    # ------------------------------------------------------------------
    # identity and global state
    # ------------------------------------------------------------------
    def identify(self) -> str:
        reply = self.command("ID")
        mnemonic, _, model = reply.partition(" ")
        if mnemonic != "ID" or not model:
            raise RadioError(f"unexpected ID reply {reply!r}")
        return model.strip()

    def set_auto_info(self, on: bool) -> None:
        self.command("AI", int(bool(on)))

    def get_control_band(self) -> int:
        return int(self.values("BC")[0])

    def set_control_band(self, band) -> None:
        self.command("BC", P.band_index(band))

    @contextlib.contextmanager
    def on_band(self, band):
        """Temporarily make `band` the control band, for commands that act on it."""
        b = P.band_index(band)
        with self._lock:
            old = self.get_control_band()
            if old != b:
                self.set_control_band(b)
            try:
                yield b
            finally:
                if old != b:
                    self.set_control_band(old)

    # ------------------------------------------------------------------
    # per-band state
    # ------------------------------------------------------------------
    def get_vfo(self, band) -> Channel:
        """The band's working buffer (what the display shows), from BUF."""
        return P.parse_buf(self.command("BUF", P.band_index(band)))[1]

    def set_vfo(self, band, ch: Channel) -> None:
        """Write the band's working buffer.

        Uses one BUF write; if this radio refuses it, falls back to the
        individual commands (FQ, SFT, OS, TO, CT, TN, CTN, REV, MD) on that band.
        """
        ch.validate()
        b = P.band_index(band)
        try:
            self.command(f"BUF {b}," + ch.spec(with_lockout=False))
        except CommandRejected as exc:
            if exc.reply != "?":
                raise
            self._set_vfo_piecewise(b, ch)

    def _set_vfo_piecewise(self, band: int, ch: Channel) -> None:
        with self.on_band(band):
            self.command("FQ", f"{ch.freq:011d}", f"{ch.step:X}")
            self.command("SFT", ch.shift)
            self.command("OS", f"{ch.offset:09d}")
            self.command("TN", f"{ch.tone_code:02d}")
            self.command("CTN", f"{ch.ctcss_code:02d}")
            self.command("TO", int(ch.tone))
            self.command("CT", int(ch.ctcss))
            self.command("REV", int(ch.reverse))
            self.command("MD", ch.mode)

    def update_vfo(self, band, **changes) -> Channel:
        """Read-modify-write of the band's buffer. Returns the new state."""
        ch = self.get_vfo(band).replace(**changes)
        self.set_vfo(band, ch)
        return self.get_vfo(band)

    def set_frequency(self, band, hz: int, step_khz: Optional[float] = None) -> Channel:
        changes = {"freq": int(hz)}
        if step_khz is not None:
            changes["step"] = P.step_index(step_khz)
        return self.update_vfo(band, **changes)

    # -- band sweep: write a frequency, let the receiver settle, read the S-meter
    def sweep_begin(self, band) -> dict:
        """Put the band in VFO mode for a sweep; returns what sweep_end restores."""
        b = P.band_index(band)
        saved = {"band": b, "mode": self.get_band_mode(b)}
        if saved["mode"] != 0:
            self.set_band_mode(b, 0)
        saved["vfo"] = self.get_vfo(b)
        return saved

    def sweep_point(self, saved: dict, hz: int, step_khz: float, settle_s: float) -> Optional[int]:
        """S-meter (0-5) at hz, or None when the radio refuses the frequency."""
        # reverse off: reverse would put the receiver on the TX frequency
        ch = saved["vfo"].replace(freq=int(hz), step=P.step_index(step_khz), reverse=False)
        try:
            self.set_vfo(saved["band"], ch)
        except (CommandRejected, ValueError):
            return None
        time.sleep(settle_s)
        return self.get_smeter(saved["band"])

    def sweep_end(self, saved: dict, tune_hz: Optional[int] = None,
                  step_khz: Optional[float] = None) -> None:
        """Put the band back as it was, or leave it in VFO on tune_hz
        (with the sweep's step, so that tune_hz is on the tuning grid)."""
        b = saved["band"]
        if tune_hz is not None:
            ch = saved["vfo"].replace(freq=int(tune_hz))
            if step_khz is not None:
                ch = ch.replace(step=P.step_index(step_khz))
            self.set_vfo(b, ch)
            return
        self.set_vfo(b, saved["vfo"])
        if saved["mode"] != 0:
            self.set_band_mode(b, saved["mode"])

    def get_band_mode(self, band) -> int:
        """0 VFO, 2 MR, 3 CALL (VMC)."""
        return int(self.values("VMC", P.band_index(band))[1])

    def set_band_mode(self, band, mode) -> None:
        if isinstance(mode, str):
            inv = {v: k for k, v in P.BAND_MODES.items()}
            mode = inv[mode.upper()]
        self.command("VMC", P.band_index(band), int(mode))

    def get_memory_channel(self, band) -> str:
        return self.values("MC", P.band_index(band))[1]

    def recall_memory(self, band, memid) -> None:
        """Put the band in MR mode on memory `memid` (000-199, L0..U9)."""
        memid = P.normalise_memid(memid)
        if P.is_call(memid):
            self.set_band_mode(band, 3)
            return
        if self.get_band_mode(band) != 2:
            self.set_band_mode(band, 2)
        self.command("MC", P.band_index(band), memid)

    def get_squelch(self, band) -> int:
        return int(self.values("SQ", P.band_index(band))[1], 16)

    def set_squelch(self, band, level: int) -> None:
        if not 0 <= int(level) <= P.SQUELCH_MAX:
            raise ValueError(f"squelch is 0-{P.SQUELCH_MAX}")
        self.command("SQ", P.band_index(band), f"{int(level):02X}")

    def get_power(self, band) -> int:
        return int(self.values("PC", P.band_index(band))[1])

    def set_power(self, band, level) -> None:
        if isinstance(level, str):
            inv = {v.lower(): k for k, v in P.POWERS.items()}
            level = inv[level.lower()]
        self.command("PC", P.band_index(band), int(level))

    def get_smeter(self, band) -> int:
        return int(self.values("SM", P.band_index(band))[1])

    def get_busy(self, band) -> bool:
        return self.values("BY", P.band_index(band))[1] == "1"

    def band_status(self, band) -> dict:
        b = P.band_index(band)
        st = {"band": P.BANDS[b], "vfo": self.get_vfo(b), "mode": self.get_band_mode(b)}
        st["memory"] = self.get_memory_channel(b) if st["mode"] == 2 else None
        st["name"] = self.read_memory_name(st["memory"]) if st["memory"] else ""
        for key, fn in (("squelch", self.get_squelch), ("power", self.get_power),
                        ("smeter", self.get_smeter), ("busy", self.get_busy)):
            try:
                st[key] = fn(b)
            except CommandRejected:
                st[key] = None
        return st

    def status(self) -> dict:
        out = {"model": self.model, "control_band": self.get_control_band()}
        with contextlib.suppress(CommandRejected):
            out["dual"] = self.values("DL")[0] == "1"
        out["bands"] = []
        for b in (0, 1):
            try:
                out["bands"].append(self.band_status(b))
            except CommandRejected:
                # single-band display: the hidden band does not answer
                out["bands"].append({"band": P.BANDS[b], "hidden": True})
        return out

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def transmit(self, on: bool, band=None) -> None:
        """Key (TX) or unkey (RX) the transmitter on the control band (or `band`)."""
        if on:
            if band is not None:
                self.set_control_band(band)
            self.command("TX")
        else:
            self.command("RX")

    def tone_1750(self, on: bool) -> None:
        """Send the 1750 Hz tone burst (TT) until RX."""
        self.command("TT" if on else "RX")

    def step(self, band, direction: int) -> None:
        """One step/channel up (+1) or down (-1) on `band`, as the knob does."""
        with self.on_band(band):
            self.command("UP" if direction > 0 else "DW")

    def scan(self, on: bool, band=None) -> None:
        if band is not None:
            self.set_control_band(band)
        self.command("SC", int(bool(on)))

    def memory_to_vfo(self, band) -> None:
        """MSH: copy the band's current memory into its VFO."""
        with self.on_band(band):
            self.command("MSH")

    # ------------------------------------------------------------------
    # memories
    # ------------------------------------------------------------------
    def read_memory(self, memid) -> Optional[Channel]:
        """Read one memory (000-199, L0..U9, CALL0/CALL1); None when empty."""
        memid = P.normalise_memid(memid)
        if P.is_call(memid):
            return self.read_call(int(memid[-1]))
        try:
            ch = P.parse_mr(self.command("MR", 0, 0, memid))[2]
        except CommandRejected as exc:
            if exc.reply == "N":
                return None
            raise
        ch.name = self.read_memory_name(memid)
        if ch.shift == 0:
            with contextlib.suppress(CommandRejected, P.ProtocolError):
                tx = P.parse_mr(self.command("MR", 0, 1, memid))[2]
                if tx.freq and tx.freq != ch.freq:
                    ch.tx_freq = tx.freq
        return ch

    def read_memory_name(self, memid) -> str:
        """The memory's name ('' when it has none or the radio refuses)."""
        try:
            reply = self.command("MNA", 0, P.normalise_memid(memid))
        except CommandRejected:
            return ""
        return reply.split(",", 2)[2] if reply.count(",") >= 2 else ""

    def write_memory(self, memid, ch: Channel) -> None:
        memid = P.normalise_memid(memid)
        if P.is_call(memid):
            return self.write_call(int(memid[-1]), ch)
        ch.validate()
        self.command(f"MW 0,0,{memid}," + ch.spec(with_lockout=True))
        time.sleep(WRITE_SETTLE)
        self.command("MNA", 0, memid, ch.name)
        if ch.tx_freq is not None and ch.shift == 0:
            time.sleep(WRITE_SETTLE)
            self.command("MW", 0, 1, memid, f"{ch.tx_freq:011d}", f"{ch.step:X}")

    def erase_memory(self, memid) -> None:
        memid = P.normalise_memid(memid)
        if P.is_call(memid):
            raise RadioError("call channels cannot be erased, only rewritten")
        self.command("MW", 0, 0, memid)

    def read_call(self, index: int) -> Channel:
        ch = P.parse_cr(self.command("CR", int(index), 0))[2]
        if ch.shift == 0:
            with contextlib.suppress(CommandRejected, P.ProtocolError):
                tx = P.parse_cr(self.command("CR", int(index), 1))[2]
                if tx.freq and tx.freq != ch.freq:
                    ch.tx_freq = tx.freq
        return ch

    def write_call(self, index: int, ch: Channel) -> None:
        ch.validate()
        self.command(f"CW {int(index)},0," + ch.spec(with_lockout=False))
        if ch.tx_freq is not None and ch.shift == 0:
            time.sleep(WRITE_SETTLE)
            self.command("CW", int(index), 1, f"{ch.tx_freq:011d}", f"{ch.step:X}")

    def vfo_to_memory(self, band, memid, name: str = "") -> Channel:
        """Store the band's current buffer in memory `memid`."""
        ch = self.get_vfo(band).replace(name=name)
        self.write_memory(memid, ch)
        return ch

    def read_all_memories(self, ids=None, progress: Optional[Callable] = None) -> dict:
        """{memid: Channel or None}. progress(done, total, memid) after each one."""
        ids = list(ids or P.memory_ids())
        out = {}
        for i, memid in enumerate(ids):
            out[memid] = self.read_memory(memid)
            if progress:
                progress(i + 1, len(ids), memid)
        return out

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def settings(self):
        return settings_for(self.model)

    def read_setting_raw(self, s: Setting) -> str:
        raw = s.raw_from_reply(self.command(s.read_command()))
        if s.kind in ("bool", "choice", "int"):
            value = s.value_field(raw)
            self._tails[s.key] = raw[len(value):]
            if s.kind != "bool":
                self._widths[s.key] = len(value)
        return raw

    def read_setting(self, s: Setting):
        return s.decode(self.read_setting_raw(s))

    def write_setting_raw(self, s: Setting, raw: str) -> None:
        width = self._widths.get(s.key)
        if width and s.kind in ("choice", "int") and raw.isdigit():
            raw = raw.zfill(width)[-width:] if len(raw) <= width else raw
        if s.kind in ("bool", "choice", "int") and "," not in raw:
            # send back the trailing fields the radio gave, unchanged
            raw += s.tail_for(raw, self._tails.get(s.key, ""))
        self.command(s.write_command(raw))

    def write_setting(self, s: Setting, value) -> None:
        self.write_setting_raw(s, s.encode(value))

    def read_all_settings(self, progress: Optional[Callable] = None) -> dict:
        """{key: raw value, or None when the radio does not offer it now}."""
        out = {}
        items = self.settings()
        for i, s in enumerate(items):
            try:
                out[s.key] = self.read_setting_raw(s)
            except (CommandRejected, ValueError):
                out[s.key] = None
            if progress:
                progress(i + 1, len(items), s.key)
        return out
