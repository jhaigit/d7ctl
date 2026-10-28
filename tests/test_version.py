import re
import subprocess

from d7ctl import __version__
from d7ctl.cli import main
from d7ctl.version import build_info, version_string


def test_build_info_in_this_checkout():
    i = build_info()
    assert i["version"] == __version__
    assert re.fullmatch(r"[0-9a-f]{7,}", i["commit"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", i["date"])
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], capture_output=True).returncode == 1
    assert i["dirty"] is dirty
    assert version_string(i).endswith(f"{'+' if dirty else ''}, {i['date']})")


def test_build_info_outside_git(tmp_path):
    i = build_info(str(tmp_path))
    assert i["commit"] is None and i["dirty"] is False
    assert version_string(i) == f"{__version__} (not a git checkout)"


def test_version_string_format():
    i = {"version": "0.1.0", "commit": "ea14338", "dirty": True, "date": "2026-10-27 22:31"}
    assert version_string(i) == "0.1.0 (ea14338+, 2026-10-27 22:31)"
    assert version_string(dict(i, dirty=False)) == "0.1.0 (ea14338, 2026-10-27 22:31)"


def test_cli_version(capsys):
    try:
        main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.startswith(f"d7ctl {__version__} (")
