# TH-D7 PC-port protocol, as d7ctl uses it

Sources (fetched 2026-10-24):
- TA1DX / KG7IL command list for the TH-D7 and TH-D7(G), qsl.net/ta1dx/kenwood/thd7kom.htm
  (the page Hamlib's TH-D7 backend cites).
- Hamlib `rigs/kenwood/th.c`, `thd7.c` (BUF, FQ, MR/MW, MNA, SM, SQ, PC, BY formats).
- CHIRP `chirp/drivers/kenwood_d7.py` (live-mode TH-D7/D7G driver: memory spec,
  call channels, program-scan IDs, the settings menu and its value lists).

Nothing in this file has yet been checked against a real radio; the
emulator (`d7ctl/emulator.py`) implements exactly what is written here.
The items under **Unverified** are the ones to check first on hardware.

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
| `BUF b[,spec]` | `BUF b,spec` | band buffer read (write: see Unverified) |
| `FQ freq,step` | | frequency of the control band |
| `SFT OS TO CT TN CTN REV MD ST` | | buffer fields of the control band |
| `VMC b[,m]` | `VMC b,m` | band mode 0 VFO, 2 MR, 3 CALL |
| `MC b[,ccc]` | `MC b,ccc` | memory on band (MR mode) |
| `SQ b[,nn]` | `SQ b,nn` | squelch 00-05 |
| `PC b[,p]` | `PC b,p` | TX power |
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

## Unverified - check on the first connection to a real radio
1. **BUF write.** The command list says BUF accepts new data. If the radio
   answers `?`, `D7Radio.set_vfo` falls back to `BC b` + FQ/SFT/OS/TO/CT/TN/CTN/REV/MD
   and restores BC - so it works either way, but note which path runs (`-v`).
2. **PC values.** Taken as 0 High, 1 Low, 2 EL; the command list says
   "0=H, 2=L, 3=EL". Read `PC 0` at each power setting on the radio.
3. **Choice widths.** `SV` (battery saver) has ten values: one digit or
   two? The driver pads writes to the width the radio used in its last
   reply, so reading a setting before writing it is always safe.
4. **DL polarity.** 1 = dual-band display on (as CHIRP uses it).
5. **TXI first value**: 0.5 min (CHIRP) vs 0.2 min (TA1DX list).
6. Replies to `MW`, `TX`, `RX`, `TT`, `UP`, `DW` - the driver accepts any
   reply starting with the mnemonic.
7. `SC`, `MON`, `NSFT` exist per the command list / Hamlib; behaviour unconfirmed.
