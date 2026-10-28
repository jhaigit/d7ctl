# d7ctl — Kenwood TH-D7 / TH-D7G control

A Python GUI and command-line program that drives a Kenwood TH-D7A, TH-D7E
or TH-D7A(G)/E(G) handheld through its serial PC port: both bands, every
menu setting, all memories (000-199, program-scan L0-U9, the two call
channels), PTT, and raw commands. It also includes a simulated radio, so
everything can be tried without one.

## Install
```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[gui,test]'      # the GUI needs PySide6; the CLI only needs pyserial
```
Your user needs access to the serial port (on Linux, the `dialout` group).
The radio's PC port is fixed at 9600 baud. Put the TNC in APRS mode or turn
it off: in PACKET mode the port belongs to the TNC.

## GUI
```sh
.venv/bin/d7ctl-gui                       # choose the port, Connect
.venv/bin/d7ctl-gui --port emulator --connect
```
- **Radio**: both bands side by side. You can set the frequency, step,
  shift and offset, tone or CTCSS, reverse, FM/AM, power, squelch,
  VFO/MR/CALL, and recall or store a memory. There is an S-meter and a
  busy light, PTT and the 1750 Hz tone (both are hold-to-transmit, with a
  3-minute safety unkey), and switches for dual band, lock, lamp, monitor,
  TX inhibit, balance and scan.
- **Memories**: a table of all 222 channels. Edit cells in place (changed
  rows turn yellow), then use Write changes. You can also erase, recall to
  band A or B, and import or export CSV in CHIRP's format.
- **Settings**: every menu setting, grouped as the radio groups them
  (Operation, Radio, DTMF, TNC, APRS, SSTV, Sky Command, programmable VFO).
  Changed settings show in bold until you write them. **Backup radio…**
  and **Restore…** save or load all memories and settings as JSON.
- **Scan**: a *band sweep* steps a band's VFO from one frequency to another;
  a *memory scan* puts each memory of a list on the VFO in turn: every
  memory, every memory in one frequency band (airband, VHF, UHF; locked-out
  memories skipped, as the radio's own scan does), or a list of your own
  (New list…, kept between runs). The S-meter (0-5) at each point is
  plotted; faint bars hold the peaks over repeated passes. On a signal of
  the chosen S or more the scan can go on, hold for a time, or hold until
  the signal drops and then a time more, so you can listen. Click the graph
  to tune there (a memory is recalled); it stops a running scan. Afterwards
  the band goes back to its mode and frequency. Each point is a BUF write,
  a dwell for the receiver to settle, and an SM read: about 3 points a
  second on a TH-D7A at 9600 baud.
- **Console**: every line sent and received, and a box for raw commands.

Every panel is a dock: drag it by its title to place panels side by side,
or float it as its own window. View > Reset layout puts them back.

## Command line
```sh
export D7_PORT=/dev/ttyUSB0               # or pass -p every time
d7ctl info                                # model and both bands
d7ctl status --watch
d7ctl freq A 145.525 --step 12.5
d7ctl tune A shift=- offset=0.6 tone=100  # keys: freq step shift offset tone ctcss rev mode
d7ctl mode B mr ; d7ctl mem recall B 12
d7ctl power A low ; d7ctl squelch B 3 ; d7ctl up A 4
d7ctl mem list ; d7ctl mem list 0-49,L0,CALL0 --all
d7ctl mem write 5 freq=146.94 shift=- offset=0.6 tone=100 name=W1AW
d7ctl mem write 21 freq=145.2 txfreq=147.45 name=ODD    # odd split (one band only)
d7ctl mem store A 30 --name HOME ; d7ctl mem erase 30-39
d7ctl mem export mem.csv ; d7ctl mem import mem.csv --dry-run
d7ctl call V freq=146.52
d7ctl setting list ; d7ctl setting describe APO
d7ctl setting set APO "30 min" ; d7ctl setting set "MP 1" "49 15.50 N 123 06.20 W"
d7ctl backup radio.json ; d7ctl restore radio.json
d7ctl tx --seconds 2                      # keys the transmitter, then always returns to RX
d7ctl raw "BUF 0" "SM 1"
d7ctl monitor                             # print the radio's auto-info reports
d7ctl shell                               # interactive, one connection
d7ctl -v ...                              # show serial traffic
```
`python -m d7ctl` is the same as `d7ctl`.

## Without a radio
`-p emulator` (or `emulator:D7` for the TH-D7A/E variant) uses a simulated
radio inside the process. `d7ctl-emulator --activity` serves one on a
pseudo-terminal, so the real serial path is exercised too:
```sh
d7ctl-emulator --activity     # prints e.g. /dev/pts/7
d7ctl-gui --port /dev/pts/7 --connect
```

## Tests
```sh
.venv/bin/python -m pytest -q
```
The tests cover the protocol codecs, the driver against the emulator
(in-process and through pyserial on a pty), the CLI, and the GUI offscreen.

## Status
Every feature is built and tested against the emulator. **None of it has
been run against a real TH-D7 yet.** The protocol comes from published
command lists and from the Hamlib and CHIRP drivers; `PROTOCOL.md` lists
them and names the details to check first on hardware.
