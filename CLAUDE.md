# d7 — working notes for Claude

Software project (not a build): Python GUI + CLI controlling a Kenwood
TH-D7 / TH-D7G handheld over its serial PC port. Started 2026-10-24.
No Redmine project and no `new-project.sh` skeleton (that skeleton is for
CAD builds). Git repository since 2026-10-24 (branch `main`); public on GitHub
since 2026-10-28 as https://github.com/jhaigit/d7ctl (remote `origin`), MIT licence.

## Releases
- `v0.1.0` (2026-10-29, commit `b299942`): first release, a GitHub Release with
  notes on what works on the TH-D7A and what has only run on the emulator.
- To release: raise the version in `pyproject.toml` and `d7ctl/__init__.py`
  together, commit, then make an annotated tag `vX.Y.Z` on that commit, push the
  tag, and run `gh release create vX.Y.Z` with notes in the same form.

## Layout
- `d7ctl/protocol.py` — tables, `Channel`, wire spec encode/decode (pure)
- `d7ctl/settings.py` — registry of every menu setting (key, kind, values, model variant)
- `d7ctl/radio.py` — `D7Radio`: port, reader thread, reply matching, high-level API
- `d7ctl/emulator.py` — simulated radio (in-process `emulator` port, or `d7ctl-emulator` on a pty)
- `d7ctl/memfile.py` — CHIRP-format CSV, JSON backup
- `d7ctl/cli.py`, `d7ctl/gui.py` (PySide6; all radio I/O on one FIFO worker thread)
- `tools/screenshots.py` — remakes the README images in `docs/` (offscreen, emulator)
- `PROTOCOL.md` — command set, sources, and the list of details unverified on hardware

## Rules
- Venv in `.venv` (PySide6-Essentials, pyserial, pytest; package installed editable).
  System Python has no tkinter, so the GUI is Qt.
- `.venv/bin/python -m pytest -q` must pass after every change; GUI tests run offscreen.
- Any protocol fact learned from the real radio: fix the code, the emulator
  (it must keep matching the radio), and PROTOCOL.md together: the answer under
  "Seen on hardware", the question removed from the Unverified list.
- PROTOCOL.md's **Unverified - open questions** list is where every open
  question about the radio lives: add one as soon as it comes up, so nothing
  is left only in a conversation.
- TX is outward-facing: the CLI only keys with an explicit `--seconds` (max 300)
  and always sends RX in `finally`; the GUI PTT is hold-to-talk with a 3-min guard.
