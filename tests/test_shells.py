"""Shells: login shells in tmux (not saved), listed with the sessions so the browser can come back to them."""
import time


def wins(snap):
    return {w["name"]: w for w in snap["windows"]}


def gone(console, name, host="local", timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if name not in wins(console.ok("GET", f"/api/sessions?host={host}&refresh=1")):
            return True
        time.sleep(0.3)
    return False


def test_shell_lifecycle(console):
    r = console.ok("POST", "/api/shells", {})
    assert r["name"] == "shell-1"
    w = wins(r)["shell-1"]
    assert w["running"] and w["shell"] and not w["saved"] and w["display"] == "Shell 1"
    assert console.ok("POST", "/api/shells", {})["name"] == "shell-2", "numbered, never reused while open"

    # a real shell you can type into
    console.ok("POST", f"/api/windows/{w['id']}/action", {"action": "send", "text": "echo SH-$((20+22))", "enter": True})
    assert "SH-42" in console.capture("shell-1", needle="SH-42")

    # not a saved session: no record, nothing to autostart
    sessions = (console.data / "users" / console.user / "boxes" / "local" / "sessions.json")
    assert not sessions.exists() or "shell-1" not in sessions.read_text()

    # `exit` closes the window and it leaves the list (no "dead" leftover)
    console.ok("POST", f"/api/windows/{w['id']}/action", {"action": "send", "text": "exit", "enter": True})
    assert gone(console, "shell-1"), "an exited shell disappears"

    # ✕ in the UI = delete
    console.ok("DELETE", "/api/sessions/shell-2")
    assert gone(console, "shell-2")
    assert any(" shell.open " in l for l in console.ok("GET", "/api/logs")["lines"])


def test_shell_folder_and_saved_sessions_are_not_shells(console):
    proj = console.work / "proj-a"
    w = wins(console.ok("POST", "/api/shells", {"cwd": str(proj)}))["shell-1"]
    assert w["cwd"] == str(proj)
    assert console.call("POST", "/api/shells", {"cwd": "/no/such/dir"})[0] == 400
    # a saved session that happens to be called shell-N is a session, not a shell
    r = console.ok("POST", "/api/sessions", {"name": "shell-9", "cwd": str(proj), "command": "cat"})
    assert not wins(r)["shell-9"]["shell"]


def test_remote_shell(console):
    console.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    r = console.ok("POST", "/api/shells?host=gpu-box", {})
    w = wins(r)["shell-1"]
    assert w["shell"] and w["cwd"] == str(console.remote_home)
    assert "shell-1" not in wins(console.ok("GET", "/api/sessions?host=local"))
    hosts = {h["id"]: h for h in console.ok("GET", "/api/hosts")["hosts"]}
    assert any(x["name"] == "shell-1" and x["shell"] for x in hosts["gpu-box"]["sessions"]), "shown in the sidebar tree"
