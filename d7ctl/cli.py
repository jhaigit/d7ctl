"""d7ctl - command-line control of a Kenwood TH-D7 / TH-D7G over its serial port.

Examples
  d7ctl ports
  d7ctl -p /dev/ttyUSB0 status
  d7ctl freq A 145.525 --step 12.5
  d7ctl tune A shift=- offset=0.6 tone=100
  d7ctl mem list
  d7ctl mem write 5 freq=146.94 shift=- offset=0.6 tone=100 name=W1AW
  d7ctl mem export memories.csv
  d7ctl setting set APO "30 min"
  d7ctl backup radio.json
  d7ctl raw "BUF 0"
  d7ctl shell

The port defaults to $D7_PORT, else /dev/ttyUSB0. Use -p emulator to try
everything against the built-in simulated radio.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time

from . import memfile
from . import protocol as P
from .radio import CommandRejected, D7Radio, RadioError
from .settings import GROUPS

TX_LIMIT_S = 300      # longest transmission the CLI will key, a stuck-PTT guard


class CliError(Exception):
    pass


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def fmt_band(st: dict, control: int) -> str:
    if st.get("hidden"):
        return f"Band {st['band']}   (not shown - single band display)"
    ch = st["vfo"]
    star = "*" if P.band_index(st["band"]) == control else " "
    mode = P.BAND_MODES.get(st["mode"], "?")
    where = f"{mode} {st['memory']}" if st["memory"] else mode
    parts = [f"Band {st['band']}{star} [{where:7}] {ch.summary()}"]
    extras = []
    if st.get("power") is not None:
        extras.append(f"power {P.POWERS.get(st['power'], st['power'])}")
    if st.get("squelch") is not None:
        extras.append(f"sql {st['squelch']}")
    if st.get("smeter") is not None:
        extras.append("S" + "#" * st["smeter"] + "." * (P.SMETER_MAX - st["smeter"]))
    if st.get("busy"):
        extras.append("BUSY")
    return parts[0] + ("\n            " + "  ".join(extras) if extras else "")


def mem_table_header() -> str:
    return (f"{'Ch':>5}  {'Name':8}  {'Freq MHz':>10}  {'Dup':5} {'Off/TX':>10}  "
            f"{'Tone':5} {'T Hz':>5} {'CT Hz':>5}  {'Step':>5} {'Mode':4} {'Rev':3} {'Skip':4}")


def mem_table_row(memid: str, ch) -> str:
    if ch is None:
        return f"{memid:>5}  (empty)"
    if ch.duplex == "split":
        off = P.format_mhz(ch.tx_freq, 4)
    elif ch.shift:
        off = P.format_mhz(ch.offset, 4)
    else:
        off = ""
    return (f"{memid:>5}  {ch.name:8}  {ch.freq / 1e6:10.5f}  {ch.duplex or '':5} {off:>10}  "
            f"{ch.tone_mode:5} {ch.tone_hz if ch.tone else '':>5} {ch.ctcss_hz if ch.ctcss else '':>5}  "
            f"{ch.step_khz:>5g} {P.MODES[ch.mode]:4} {'R' if ch.reverse else '':3} "
            f"{'S' if ch.lockout else '':4}")


def parse_edits(items) -> dict:
    edits = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise CliError(f"expected key=value, got {item!r} (keys: {', '.join(P.CHANNEL_KEYS)})")
        edits[key.strip()] = value
    return edits


def parse_ids(text: str):
    """'0-9,20,L0,CALL0' or 'all' -> list of memids."""
    if text in (None, "", "all"):
        return P.memory_ids()
    out = []
    for part in text.split(","):
        part = part.strip()
        a, sep, b = part.partition("-")
        if sep and a.isdigit() and b.isdigit():
            out += [f"{n:03d}" for n in range(int(a), int(b) + 1)]
        else:
            out.append(P.normalise_memid(part))
    return out


def progress_printer(label):
    def fn(done, total, item):
        if sys.stderr.isatty():
            sys.stderr.write(f"\r{label} {done}/{total} {item:<8}")
            if done == total:
                sys.stderr.write("\n")
            sys.stderr.flush()
    return fn


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_ports(radio, a):
    from serial.tools import list_ports
    # Linux lists every legacy 8250 UART slot (/dev/ttyS0-31) whether or not
    # hardware is behind it; those report no description ("n/a").
    ports = [p for p in list_ports.comports() if p.description not in ("", "n/a")]
    if not ports:
        print("no serial ports found (USB adapters appear as /dev/ttyUSB*, /dev/ttyACM*)")
    for p in ports:
        print(f"{p.device:20} {p.description}")
    print(f"{'emulator':20} built-in simulated TH-D7G")


def cmd_info(radio, a):
    print(f"Model: {radio.model}   port {radio.port_name}")
    st = radio.status()
    if "dual" in st:
        print(f"Dual band: {'on' if st['dual'] else 'off'}   control band: {P.BANDS[st['control_band']]}")
    for b in st["bands"]:
        print(fmt_band(b, st["control_band"]))


def cmd_status(radio, a):
    if not a.watch:
        return cmd_info(radio, a)
    try:
        while True:
            st = radio.status()
            sys.stdout.write("\x1b[2J\x1b[H")
            print(time.strftime("%H:%M:%S"), radio.model, "(ctrl-C to stop)")
            for b in st["bands"]:
                print(fmt_band(b, st["control_band"]))
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()


def cmd_band(radio, a):
    if a.band:
        radio.set_control_band(a.band)
    print(f"control band {P.BANDS[radio.get_control_band()]}")


def cmd_freq(radio, a):
    if a.freq:
        ch = radio.set_frequency(a.band, P.parse_frequency(a.freq), a.step)
    else:
        ch = radio.get_vfo(a.band)
    print(f"{P.BANDS[P.band_index(a.band)]}: {ch.summary()}")


def cmd_tune(radio, a):
    b = P.band_index(a.band)
    ch = radio.get_vfo(b)
    if a.edits:
        new = P.apply_channel_edits(ch, parse_edits(a.edits))
        if new.tx_freq is not None:
            raise CliError("odd split (txfreq) exists only in memories and call channels")
        radio.set_vfo(b, new)
        ch = radio.get_vfo(b)
    print(f"{P.BANDS[b]}: {ch.summary()}")


def cmd_mode(radio, a):
    if a.mode:
        radio.set_band_mode(a.band, a.mode)
    m = radio.get_band_mode(a.band)
    extra = f" {radio.get_memory_channel(a.band)}" if m == 2 else ""
    print(f"{P.BANDS[P.band_index(a.band)]}: {P.BAND_MODES.get(m, m)}{extra}")


def cmd_power(radio, a):
    if a.level:
        radio.set_power(a.band, a.level)
    print(f"{P.BANDS[P.band_index(a.band)]}: power {P.POWERS[radio.get_power(a.band)]}")


def cmd_squelch(radio, a):
    if a.level is not None:
        radio.set_squelch(a.band, a.level)
    print(f"{P.BANDS[P.band_index(a.band)]}: squelch {radio.get_squelch(a.band)}")


def cmd_meter(radio, a):
    b = P.band_index(a.band)
    print(f"{P.BANDS[b]}: S{radio.get_smeter(b)} {'BUSY' if radio.get_busy(b) else 'quiet'}")


def cmd_updown(radio, a):
    for _ in range(a.count):
        radio.step(a.band, +1 if a.cmd == "up" else -1)
    print(f"{P.BANDS[P.band_index(a.band)]}: {radio.get_vfo(a.band).summary()}")


def _timed_key(radio, a, start, what):
    if not 0 < a.seconds <= TX_LIMIT_S:
        raise CliError(f"--seconds must be between 0 and {TX_LIMIT_S}")
    print(f"{what} for {a.seconds:g} s on band {P.BANDS[radio.get_control_band()]} ... ", end="", flush=True)
    try:
        start()
        time.sleep(a.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        radio.transmit(False)
    print("back to receive")


def cmd_tx(radio, a):
    if a.band:
        radio.set_control_band(a.band)
    _timed_key(radio, a, lambda: radio.transmit(True), "transmitting")


def cmd_rx(radio, a):
    radio.transmit(False)
    print("receive")


def cmd_tone1750(radio, a):
    _timed_key(radio, a, lambda: radio.tone_1750(True), "1750 Hz tone")


def cmd_scan(radio, a):
    radio.scan(a.state == "on", a.band)
    print(f"scan {a.state}")


def cmd_mem(radio, a):
    sub = a.mem_cmd
    if sub == "list":
        print(mem_table_header())
        ids = parse_ids(a.range)
        for memid in ids:
            ch = radio.read_memory(memid)
            if ch is not None or a.all:
                print(mem_table_row(memid, ch), flush=True)
    elif sub == "read":
        memid = P.normalise_memid(a.memid)
        print(mem_table_header())
        print(mem_table_row(memid, radio.read_memory(memid)))
    elif sub == "write":
        memid = P.normalise_memid(a.memid)
        base = None if a.new else radio.read_memory(memid)
        if base is None:
            if "freq" not in [e.partition("=")[0] for e in a.edits]:
                raise CliError(f"{memid} is empty: give at least freq=")
            base = P.Channel(step=3)
        ch = P.apply_channel_edits(base, parse_edits(a.edits))
        radio.write_memory(memid, ch)
        print(mem_table_header())
        print(mem_table_row(memid, radio.read_memory(memid)))
    elif sub == "erase":
        for memid in parse_ids(a.memid):
            radio.erase_memory(memid)
            print(f"{memid} erased")
    elif sub == "recall":
        radio.recall_memory(a.band, a.memid)
        print(f"{P.BANDS[P.band_index(a.band)]}: {radio.get_vfo(a.band).summary()}")
    elif sub == "store":
        ch = radio.vfo_to_memory(a.band, a.memid, a.name or "")
        print(f"{P.normalise_memid(a.memid)} <- band {a.band.upper()}: {ch.summary()}")
    elif sub == "to-vfo":
        radio.memory_to_vfo(a.band)
        print(f"{P.BANDS[P.band_index(a.band)]}: {radio.get_vfo(a.band).summary()}")
    elif sub == "export":
        mems = radio.read_all_memories(parse_ids(a.range), progress_printer("reading"))
        n = memfile.export_csv(a.file, mems)
        print(f"{n} memories written to {a.file}")
    elif sub == "import":
        mems, warnings = memfile.import_csv(a.file)
        for w in warnings:
            print("warning:", w, file=sys.stderr)
        if a.erase_missing:
            missing = [m for m in P.memory_ids() if m not in mems and not P.is_call(m)]
        else:
            missing = []
        print(f"{len(mems)} memories in {a.file}" + (f", {len(missing)} slots to erase" if missing else ""))
        if a.dry_run:
            print(mem_table_header())
            for memid, ch in mems.items():
                print(mem_table_row(memid, ch))
            return
        for i, (memid, ch) in enumerate(mems.items()):
            radio.write_memory(memid, ch)
            progress_printer("writing")(i + 1, len(mems), memid)
        for memid in missing:
            radio.erase_memory(memid)
        print("done")


def cmd_call(radio, a):
    idx = 0 if a.which.upper() in ("V", "VHF", "0") else 1
    if a.edits:
        ch = P.apply_channel_edits(radio.read_call(idx), parse_edits(a.edits))
        radio.write_call(idx, ch)
    print(f"{P.CALL_LABELS[P.CALL_IDS[idx]]}: {radio.read_call(idx).summary()}")


def cmd_setting(radio, a):
    if a.set_cmd == "list":
        for group in GROUPS:
            if a.group and a.group.lower() != group.lower():
                continue
            items = [s for s in radio.settings() if s.group == group]
            if not items:
                continue
            print(f"[{group}]")
            for s in items:
                try:
                    value = radio.read_setting(s)
                    if isinstance(value, bool):
                        value = "on" if value else "off"
                except (CommandRejected, ValueError):
                    value = "(not available now)"
                print(f"  {s.key:8} {s.label:38} {value}")
    elif a.set_cmd == "get":
        s = _find_setting(radio, a.key)
        print(f"{s.key} = {radio.read_setting(s)}")
    elif a.set_cmd == "set":
        s = _find_setting(radio, a.key)
        radio.write_setting(s, a.value)
        print(f"{s.key} = {radio.read_setting(s)}")
    elif a.set_cmd == "describe":
        for s in radio.settings():
            if a.key and s.key != " ".join(a.key.upper().split()):
                continue
            print(f"{s.key:8} {s.group:16} {s.label:38} {s.describe_values()}"
                  + (f"   ({s.note})" if s.note else ""))


def _find_setting(radio, key):
    wanted = " ".join(key.upper().split())
    for s in radio.settings():
        if s.key == wanted:
            return s
    raise CliError(f"no setting {key!r} on {radio.model}; see 'd7ctl setting describe'")


def cmd_backup(radio, a):
    mems = radio.read_all_memories(progress=progress_printer("memories"))
    settings = radio.read_all_settings(progress=progress_printer("settings"))
    memfile.write_backup(a.file, radio.model, mems, settings)
    n = sum(1 for v in mems.values() if v is not None)
    print(f"{n} memories and {sum(v is not None for v in settings.values())} settings saved to {a.file}")


def cmd_restore(radio, a):
    doc = memfile.read_backup(a.file)
    if doc["model"] != radio.model and not a.force:
        raise CliError(f"backup is from a {doc['model']}, this is a {radio.model} (use --force)")
    if not a.no_memories:
        mems = doc["memories"]
        for i, (memid, ch) in enumerate(mems.items()):
            radio.write_memory(memid, ch)
            progress_printer("memories")(i + 1, len(mems), memid)
        for memid in doc.get("empty_memories", []):
            if not P.is_call(memid):
                radio.erase_memory(memid)
    failed = []
    if not a.no_settings:
        by_key = {s.key: s for s in radio.settings()}
        for key, raw in doc["settings"].items():
            s = by_key.get(key)
            if s is None or not s.restore:
                continue
            try:
                radio.write_setting_raw(s, raw)
            except RadioError:
                failed.append(key)
    print("restored" + (f"; settings the radio refused: {', '.join(failed)}" if failed else ""))


def cmd_raw(radio, a):
    for line in a.line:
        print(radio.command(line, check=False))


def cmd_monitor(radio, a):
    radio.add_event_listener(lambda line: print(time.strftime("%H:%M:%S"), line, flush=True))
    radio.set_auto_info(True)
    print("auto-info on; turn the knob or wait for a signal (ctrl-C to stop)")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        radio.set_auto_info(False)


def cmd_shell(radio, a):
    print(f"{radio.model} on {radio.port_name}. Type d7ctl commands without 'd7ctl' "
          "(e.g. 'status', 'mem list 0-9'), '!<raw command>' to send a raw line, 'quit' to leave.")
    parser = build_parser()
    while True:
        try:
            line = input("d7> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return
        if line.startswith("!"):
            print(radio.command(line[1:].strip(), check=False))
            continue
        try:
            sub = parser.parse_args(shlex.split(line))
            if sub.cmd in ("shell", "gui", "ports", None):
                print("not available inside the shell")
                continue
            sub.func(radio, sub)
        except SystemExit:
            pass
        except (RadioError, CliError, ValueError, KeyError) as exc:
            print("error:", exc)


def cmd_gui(radio, a):
    try:
        from .gui import main as gui_main
    except ModuleNotFoundError as e:
        if e.name is None or not e.name.startswith("PySide6"):
            raise
        sys.exit("the GUI needs PySide6: pip install PySide6-Essentials "
                 "(or pip install -e '.[gui]')")
    gui_main([] if a.port is None else ["--port", a.port])


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="d7ctl", description="Control a Kenwood TH-D7 / TH-D7G over its serial port.",
        epilog=__doc__.split("Examples", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default=None,
                    help="serial port, URL or 'emulator' (default $D7_PORT or /dev/ttyUSB0)")
    ap.add_argument("-b", "--baud", type=int, default=P.BAUDRATE, help="baud rate (the D7 uses 9600)")
    ap.add_argument("--timeout", type=float, default=1.0, help="reply timeout, s")
    ap.add_argument("-v", "--verbose", action="store_true", help="print serial traffic to stderr")
    sp = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, func, help_, **kw):
        p = sp.add_parser(name, help=help_, description=help_, **kw)
        p.set_defaults(func=func)
        return p

    add("ports", cmd_ports, "list serial ports").set_defaults(no_radio=True)
    add("info", cmd_info, "model and both bands")
    p = add("status", cmd_status, "both bands; --watch to keep refreshing")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=float, default=1.0)
    p = add("band", cmd_band, "show or set the control band")
    p.add_argument("band", nargs="?", choices=["A", "B", "a", "b"])
    p = add("freq", cmd_freq, "show or set a band's frequency (MHz, or with k/M suffix)")
    p.add_argument("band")
    p.add_argument("freq", nargs="?")
    p.add_argument("--step", type=float, help="tuning step, kHz")
    p = add("tune", cmd_tune, "show or edit a band's VFO: key=value ... "
            "(freq step shift offset tone ctcss rev mode)")
    p.add_argument("band")
    p.add_argument("edits", nargs="*")
    p = add("mode", cmd_mode, "show or set a band's mode: vfo, mr, call")
    p.add_argument("band")
    p.add_argument("mode", nargs="?", type=str.upper, choices=["VFO", "MR", "CALL"])
    p = add("power", cmd_power, "show or set TX power: high, low, el")
    p.add_argument("band")
    p.add_argument("level", nargs="?", type=str.lower, choices=["high", "low", "el"])
    p = add("squelch", cmd_squelch, "show or set squelch 0-5")
    p.add_argument("band")
    p.add_argument("level", nargs="?", type=int)
    p = add("meter", cmd_meter, "S-meter and busy for a band")
    p.add_argument("band")
    for name in ("up", "down"):
        p = add(name, cmd_updown, f"one step/channel {name} (as the knob)")
        p.add_argument("band")
        p.add_argument("count", nargs="?", type=int, default=1)
    p = add("tx", cmd_tx, "transmit for --seconds, then return to receive")
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--band", help="make this the control band first")
    add("rx", cmd_rx, "return to receive (stop TX or 1750 Hz tone)")
    p = add("tone1750", cmd_tone1750, "send the 1750 Hz tone for --seconds")
    p.add_argument("--seconds", type=float, default=1.0)
    p = add("scan", cmd_scan, "start/stop scanning")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--band")

    p = add("mem", cmd_mem, "memories: list read write erase recall store to-vfo export import")
    ms = p.add_subparsers(dest="mem_cmd", metavar="ACTION", required=True)
    q = ms.add_parser("list", help="list memories (RANGE like 0-49,L0,CALL0; default all)")
    q.add_argument("range", nargs="?")
    q.add_argument("--all", action="store_true", help="show empty slots too")
    q = ms.add_parser("read", help="one memory")
    q.add_argument("memid")
    q = ms.add_parser("write", help="write a memory: CH key=value ... "
                      "(freq step shift offset tone ctcss rev mode lockout name txfreq)")
    q.add_argument("memid")
    q.add_argument("edits", nargs="+")
    q.add_argument("--new", action="store_true", help="start from blank rather than the current contents")
    q = ms.add_parser("erase", help="erase memories (CH or RANGE)")
    q.add_argument("memid")
    q = ms.add_parser("recall", help="put a band on a memory: BAND CH")
    q.add_argument("band")
    q.add_argument("memid")
    q = ms.add_parser("store", help="store a band's VFO in a memory: BAND CH [--name]")
    q.add_argument("band")
    q.add_argument("memid")
    q.add_argument("--name")
    q = ms.add_parser("to-vfo", help="copy a band's current memory to its VFO")
    q.add_argument("band")
    q = ms.add_parser("export", help="save memories to a CHIRP-format CSV")
    q.add_argument("file")
    q.add_argument("--range")
    q = ms.add_parser("import", help="load memories from a CHIRP-format CSV")
    q.add_argument("file")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--erase-missing", action="store_true",
                   help="erase memories that are not in the file")

    p = add("call", cmd_call, "show or edit a call channel: V|U [key=value ...]")
    p.add_argument("which", choices=["V", "U", "v", "u", "VHF", "UHF", "0", "1"])
    p.add_argument("edits", nargs="*")

    p = add("setting", cmd_setting, "menu settings: list get set describe")
    ss = p.add_subparsers(dest="set_cmd", metavar="ACTION", required=True)
    q = ss.add_parser("list", help="every setting with its current value")
    q.add_argument("--group", help="one of: " + ", ".join(GROUPS))
    q = ss.add_parser("get")
    q.add_argument("key")
    q = ss.add_parser("set")
    q.add_argument("key")
    q.add_argument("value")
    q = ss.add_parser("describe", help="settings and their allowed values (no radio needed)")
    q.add_argument("key", nargs="?")

    p = add("backup", cmd_backup, "save all memories and settings to JSON")
    p.add_argument("file")
    p = add("restore", cmd_restore, "write a JSON backup to the radio")
    p.add_argument("file")
    p.add_argument("--no-memories", action="store_true")
    p.add_argument("--no-settings", action="store_true")
    p.add_argument("--force", action="store_true", help="allow a backup from the other model")
    p = add("raw", cmd_raw, "send raw command lines, print the replies")
    p.add_argument("line", nargs="+")
    add("monitor", cmd_monitor, "turn auto-info on and print what the radio reports")
    add("shell", cmd_shell, "interactive session on one connection")
    add("gui", cmd_gui, "open the graphical interface").set_defaults(no_radio=True)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    if a.cmd == "setting" and a.set_cmd == "describe" and a.port is None:
        # describing the table needs no radio
        from .settings import settings_for

        class _Offline:
            def settings(self):
                return settings_for(None)
        a.func(_Offline(), a)
        return 0
    if getattr(a, "no_radio", False):
        a.func(None, a)
        return 0
    port = a.port or os.environ.get("D7_PORT") or "/dev/ttyUSB0"
    radio = D7Radio(port, baudrate=a.baud, timeout=a.timeout)
    if a.verbose:
        radio.add_traffic_listener(lambda d, line: print(f"{d} {line}", file=sys.stderr))
    try:
        radio.open()
    except Exception as exc:     # serial.SerialException, RadioError, OSError
        print(f"d7ctl: cannot talk to a TH-D7 on {port}: {exc}", file=sys.stderr)
        return 1
    try:
        a.func(radio, a)
        return 0
    except (RadioError, CliError, ValueError, KeyError) as exc:
        print(f"d7ctl: {exc}", file=sys.stderr)
        return 1
    finally:
        radio.close()


if __name__ == "__main__":
    sys.exit(main())
