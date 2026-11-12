"""Real browser checks (Playwright via Node): desktop + mobile UI smoke test, and an opt-in leak/CPU probe.

Setup once:  (cd tests/browser && npm install)   — uses an installed Chromium (set CHROME=/path/to/chrome
             or install one with `npx playwright install chromium`).
Leak/CPU probe (90 s):  DEVBOX_PERF=1 pytest tests/test_browser.py
"""
import glob
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent / "browser"
node_ok = shutil.which("node") and (HERE / "node_modules" / "playwright-core").exists()
pytestmark = pytest.mark.skipif(not node_ok, reason="node + tests/browser/node_modules (npm install) needed")


def chrome() -> str | None:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")))
    return found[-1] if found else None


def seed(c):
    """The state the UI scripts click through: local joseph + waiter, remote gpu-box with a busy 'trainer'.

    'trainer' fills the screen first: xterm's DOM renderer adds an element per non-empty row, so a half-empty
    screen would look like DOM growth to the leak probe."""
    proj = str(c.work / "proj-a")
    c.ok("POST", "/api/sessions", {"name": "joseph", "display": "Joseph", "cwd": proj,
                                   "command": "bash -c 'echo hello from joseph; sleep 600'"})
    c.ok("POST", "/api/sessions", {"name": "waiter", "cwd": proj,
                                   "command": "bash -c 'printf \"Do you want to proceed?\\n❯ 1. Yes\\n\"; sleep 600'"})
    c.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    c.ok("POST", "/api/sessions?host=gpu-box", {"name": "trainer", "cwd": "~",
                                                "command": "bash -c 'seq 1 200; i=0; while :; do i=$((i+1)); echo step $i; sleep 1; done'"})
    c.window("trainer", host="gpu-box")


def run_node(c, script, tmp_path, timeout):
    env = {**os.environ, "DEVBOX_URL": c.url, "DEVBOX_COOKIE": c.cookie_value, "OUT": str(tmp_path / "shots")}
    if chrome():
        env["CHROME"] = chrome()
    p = subprocess.run(["node", str(HERE / script)], env=env, capture_output=True, text=True, timeout=timeout)
    print(p.stdout)
    assert p.returncode == 0, p.stdout + p.stderr


def test_ui_smoke_desktop_and_mobile(console, tmp_path):
    seed(console)
    run_node(console, "shots.js", tmp_path, 180)


@pytest.mark.skipif(os.environ.get("DEVBOX_PERF") != "1", reason="set DEVBOX_PERF=1 (takes ~2 min)")
def test_no_leak_low_cpu(console, tmp_path):
    seed(console)
    run_node(console, "perf.js", tmp_path, 400)
