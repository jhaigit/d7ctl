"""Version, git commit and date of the running code, for About and --version.

The commit comes from git at run time, so a checkout run in place (as on the
host, from the shared folder) always reports what it is actually running.
Outside a git checkout, or without git, only the version is known.
"""

from __future__ import annotations

import os
import subprocess

from . import __version__

GIT_TIMEOUT_S = 2.0          # never hold up start-up for a slow or hung git
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S)


def build_info(path: str = PACKAGE_DIR) -> dict:
    """{"version", "commit" (short id or None), "dirty" (bool), "date" (commit date or None)}."""
    info = {"version": __version__, "commit": None, "dirty": False, "date": None}
    try:
        head = _git(path, "log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M")
        if head.returncode != 0 or not head.stdout.strip():
            return info
        info["commit"], info["date"] = head.stdout.strip().split(" ", 1)
        # uncommitted changes to tracked files (untracked files do not count, as in git describe --dirty)
        info["dirty"] = _git(path, "diff", "--quiet", "HEAD", "--").returncode == 1
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return info


def version_string(info: dict | None = None) -> str:
    """'0.1.0 (ea14338+, 2026-10-27 22:31)'; '+' = uncommitted changes."""
    info = info or build_info()
    if info["commit"] is None:
        return f"{info['version']} (not a git checkout)"
    return f"{info['version']} ({info['commit']}{'+' if info['dirty'] else ''}, {info['date']})"
