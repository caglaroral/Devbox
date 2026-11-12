"""Unit tests for the "is Claude Code working?" heuristic behind the green indicator."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="devbox-unit-")
os.environ.setdefault("DEVBOX_DATA", os.path.join(_tmp, "data"))
os.environ.setdefault("DEVBOX_RUNTIME", os.path.join(_tmp, "run"))
os.environ.pop("VAULT_ADDR", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from devbox_console.app import classify, login_cmd, mask  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize("screen,expected", [
    ("✻ Thinking… (esc to interrupt)", "working"),
    ("some output\n✶ Pondering… (3m 19s · ↓ 1.2k tokens)", "working"),
    ("⠋ Running 2 shell commands", "working"),
    ("Edit file?\nDo you want to proceed?\n❯ 1. Yes\n  2. No", "waiting"),
    ("Overwrite? [y/N]", "waiting"),
    ("> \n  ? for shortcuts", "idle"),
])
def test_classify_claude_screens(screen, expected):
    assert classify(screen, "claude", changed=False) == expected


def test_changed_content_counts_as_working():
    assert classify("plain output", "claude", changed=True) == "working"


def test_shell_window_is_shell():
    assert classify("$ ", "bash", changed=False) == "shell"


def test_waiting_beats_working():
    assert classify("esc to interrupt\nDo you want to proceed?", "claude", changed=True) == "waiting"


def test_only_tail_of_screen_counts():
    old = "Do you want to proceed?\n" + "\n".join(f"line {i}" for i in range(40))
    assert classify(old, "claude", changed=False) == "idle"


def test_mask_and_login_cmd():
    assert mask("sk-ant-1234567890") == "••••••••7890"
    assert mask("abc") == "•••"
    assert login_cmd("claude --continue") == 'exec "${SHELL:-/bin/bash}" -lc \'claude --continue\''
