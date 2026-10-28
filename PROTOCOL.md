# TH-D7 PC-port protocol, as d7ctl uses it

Sources (fetched 2026-10-24):
- TA1DX / KG7IL command list for the TH-D7 and TH-D7(G), qsl.net/ta1dx/kenwood/thd7kom.htm
  (the page Hamlib's TH-D7 backend cites).
- Hamlib `rigs/kenwood/th.c`, `thd7.c` (BUF, FQ, MR/MW, MNA, SM, SQ, PC, BY formats).
- CHIRP `chirp/drivers/kenwood_d7.py` (live-mode TH-D7/D7G driver: memory spec,
  call channels, program-scan IDs, the settings menu and its value lists).

First checked against a real radio on 2026-10-27 (a TH-D7A, `ID TH-D7`,
PG-4W-type cable on /dev/ttyUSB0); see **Seen on hardware**. The emulator
(`d7ctl/emulator.py`) implements exactly what is written here. The items
under **Unverified** are the ones still to check.

## Link
9600 baud, 8N1, no handshake, on the 2.5 mm PC jack (tip = data from
radio, ring = data to radio, sleeve = ground; a PG-4W or a 3.3/5 V
USB-serial adapter with an RS-232 level shifter). The TNC must not be in
PACKET mode: then the port belongs to the TNC. APRS mode or TNC off is fine.

Commands are ASCII lines ending in CR: mnemonic, one space, comma-separated
arguments. Every command gets exactly one CR-terminated reply: the
mnemonic with its current value, `?` (not understood) or `N` (not possible
now, e.g. an empty memory). With `AI 1` the radio also sends unsolicited
lines (`BUF`, `BY`, `SM`, `BC`, ...) when things change.

## Channel spec (BUF, MR/MW, CR/CW)
`freq(11),step(hex),shift,rev,tone,ctcss,<dcs: empty>,tone#(2),<empty>,ctcss#(2),offset(9),mode[,lockout]`

| field | values |
|---|---|
| step | 0-9: 5, 6.25, 10, 12.5, 15, 20, 25, 30, 50, 100 kHz |
| shift | 0 simplex, 1 +, 2 -, 3 -7.6 MHz (TH-D7E) |
| tone#, ctcss# | 01-39, table in `protocol.TONES_HZ`; 02 (69.3) not offered |
| mode | 0 FM, 1 AM |
| lockout | memories only |

## Commands used

| Command | Reply | Meaning |
|---|---|---|
| `ID` | `ID TH-D7` / `ID TH-D7G` | model (A/E report TH-D7) |
| `AI n` | `AI n` | auto-information off/on |
| `BC [b]` | `BC b` | control band 0 A / 1 B |
| `BUF b[,spec]` | `BUF b,spec` | band buffer read and write |
| `FQ freq,step` | | frequency of the control band |
| `SFT OS TO CT TN CTN REV MD ST` | | buffer fields of the control band |
| `VMC b[,m]` | `VMC b,m` | band mode 0 VFO, 2 MR, 3 CALL |
| `MC b[,ccc]` | `MC b,ccc` | memory on band (MR mode) |
| `SQ b[,nn]` | `SQ b,nn` | squelch 00-05 |
| `PC b[,p]` | `PC b,p` | TX power: 0 High, 2 Low, 3 EL |
| `SM b` / `BY b` | `SM b,n` / `BY b,0/1` | S-meter 0-5 / busy |
| `TX` `RX` `TT` | | key / unkey / 1750 Hz tone |
| `UP` `DW` | | knob step on the control band |
| `SC n` | | scan stop/start |
| `MSH` | | memory to VFO |
| `MR 0,s,ccc` | `MR 0,s,ccc,spec,lockout` | memory read; s=1 gives the odd-split TX side; `N` = empty |
| `MW 0,s,ccc[,spec]` | | memory write; no spec = erase; s=1 writes `txfreq,step` |
| `MNA 0,ccc[,name]` | `MNA 0,ccc,name` | memory name, 8 chars |
| `CR i,s` / `CW i,s,spec` | | call channel 0 VHF / 1 UHF |
| memory ids | | `000`-`199`, program scan `L0`..`L9`, `U0`..`U9` |
| `DM nn` / `DMN nn` | | DTMF memory 00-09 digits (`*`=E, `#`=F) / name |
| `MP [n]` | 17 digits `ddmmhh0Sdddmmhh0W` | position (D7A: no index; D7G: 1-3) |
| `PV n` | `lllll,uuuuu` | programmable VFO limits, MHz; n = 1, 2, 3, 6 |

Every other menu setting is a plain get/set of one value; the full list
with value tables is `d7ctl/settings.py` (`d7ctl setting describe`).

## Seen on hardware (TH-D7A, 2026-10-27)
- `ID` -> `ID TH-D7`; `AI 0`, `BC`, `DL`, `BUF 0`, `VMC`, `SQ`, `PC`, `SM`, `BY`
  all answer in the formats above. `BUF 0` with an empty DCS and an empty field
  as documented: `BUF 0,00156800000,0,0,0,0,0,,23,,23,000000000,0`.
- **BUF write works** and echoes the new buffer: `BUF 0,00122800000,0,0,0,0,0,,01,,01,000000000,1`
  -> the same line, and `BUF 0` then reads it back. AM (mode 1) on 122.7 MHz
  decodes and re-encodes unchanged.
- With `DL 0` (single band), `BUF 1` answers `N`: the hidden band does not answer.
- Some settings carry fields after the value: `APO 1,0` and `BEL 0,0,` (an
  empty last field). The driver decodes the first field and writes back the
  trailing fields as last read (`Setting.tail` when nothing was read yet).
  What APO's second field means is not known.
- A full backup read 61 memories and 76 of the 82 settings registered for
  the TH-D7. Not answered: `PC 1`, `ASC 1`, `BEL 1` (band B hidden, expected)
  and `TH` (`N`: exists, refused in the state the radio was in), `BEPT` and
  `TEMP` (`?`: not on the TH-D7A, so now TH-D7G only).
- Menu values read off the display against the replies:
  `APO` `0` Off (no second field), `1,0` 30 min, `2,0` 60 min; `TXI` 0-7 = 0.5, 1, 2, 3, 5, 10,
  20, 30 min (no 4 min, unlike the first table); `SV` 0 Off, 1 0.2 s,
  5 1.0 s, 9 5 s; `DL` 0 single band, 1 dual. The TH-D7A has no 1750 Hz
  menu item, and answers `N` to `TH` and to `TT` (no tone is sent). The GUI
  greys out its 1750 Hz button when `TH` is refused.
- `RX` answers a bare `RX` (no argument).
- Memory names (2026-10-28): lower case and most punctuation are stored as
  sent, `;` included (`MNA 0,199,Test;x`), and leading spaces are kept
  (`MNA 0,199,  Testx` reads back the same). d7ctl therefore never trims
  names: the reader removes only CR/LF from a line, never spaces.
- Every printable ASCII punctuation character is stored and has a glyph on
  the display, except the comma (the field separator), which the name
  editor refuses.
- Trailing spaces are kept too (2026-10-28): `MNA 0,199,X   ` reads back
  with its three spaces. A name entered on the keys had none (`BA2uA`).
- A channel write `MW 0,0,ccc,<spec>` answers a bare `MW` (2026-10-28).
- An empty name clears it: `MNA 0,050,` (as `write_memory` sends for a
  channel without a name).
- A name entered on the radio's keys ([F], [9]) is reported with AI on as
  `MNA 0,ccc,name` when the editor closes (2026-10-28: `MNA 0,199,BA2uA`).
  The GUI updates that memory's row, its lists and the band display.
- `MR 0,1,ccc` (the odd-split side) answers `N` for a memory with no split.
- A command arriving on the PC port cancels the radio's menu mode, so
  polling (the GUI's Poll meters) makes the menus unusable.
- With the TNC in PACKET mode the TNC answers instead: `ID` gives `cmd:ID`
  (its prompt and an echo) then `?EH`. `D7Radio.open` reports this as
  `TncPacketMode` instead of a plain timeout.
- **PC codes are 0 High, 2 Low, 3 EL** (the TA1DX list; not 0-1-2 as first
  coded): the radio answered `PC 0,2` with L on its display.
  Code 1 is refused: `PC 0,1` -> `N`; `PC 0,2` -> `PC 0,2`.

## Unverified - open questions
Questions still to answer on a real radio. When one is answered, record the
answer under **Seen on hardware** (fixing the code and the emulator to
match) and remove it here. Numbers are not stable; refer to the titles.

**Not yet tried on the radio**
- **Transmit**: `d7ctl tx --seconds N` and the GUI PTT (TX, then RX in all
  cases). The reply to `TX` is unknown; `RX` answers `RX`. Use a dummy load.
- **Band sweep** (GUI Scan): how long after a BUF write the S-meter reading
  is valid (default dwell 100 ms, a guess), and whether `SM` reads a signal
  below the squelch setting or reports 0 until the squelch opens.
- **`TC 1`** (from the Kenwood command lists) should hand the port from the
  TNC in PACKET mode back to the radio; the emulator assumes it does.
- **BUF-write fallback**: `D7Radio.set_vfo` falls back to FQ/SFT/OS/... if BUF
  write answers `?`. The TH-D7A accepts BUF write, so the fallback has never run.

**Partly answered**
- **APO second field**: present only when APO is on (`APO 0`, `APO 1,0`),
  always 0 so far; meaning unknown. The driver writes the forms it reads;
  whether the radio also accepts `APO 1` alone is untested.
- **Memory name characters above 127**, e.g. `Café` - stored, changed or
  refused? ASCII punctuation and spaces are settled (see above).
- Replies to `MW` for the split side (`MW 0,1,...`) and for an erase, and to
  `TX`, `UP`, `DW`: the driver accepts any reply starting with the mnemonic;
  the exact forms are not recorded yet.
- `SC`, `MON`, `NSFT` exist per the command list / Hamlib; behaviour unconfirmed.

**Other models**
- **TH-D7E / TH-D7(G)**: `TH` (1750 Hz hold) and `TT` on a radio that has the
  tone; all D7G-only settings (none tested: the radio at hand is a TH-D7A).
