"""Local sessions end to end: auth, session CRUD, hide/unhide, external tmux sessions, env, messages."""
import time


def names(snap):
    return {w["name"]: w for w in snap["windows"]}


def test_auth_required(console):
    assert console.call("GET", "/api/windows", auth=False)[0] == 401
    st, _ = console.call("POST", "/api/login", {"username": "root", "password": "x"}, auth=False)
    assert st in (401, 403)
    who = console.ok("GET", "/api/whoami")
    assert who["user"] == console.user and who["auth"] == "pam"
    from devbox_console import __version__
    assert who["version"] == __version__ and who["commit"], "version + git commit are reported"


def test_origin_check(console):
    import urllib.request, urllib.error
    req = urllib.request.Request(console.url + "/api/sessions", method="POST", data=b"{}",
                                 headers={"Cookie": console.cookie, "Origin": "https://evil.example",
                                          "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
        assert False, "cross-origin POST accepted"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_session_crud(console):
    proj = str(console.work / "proj-a")
    console.ok("PUT", "/api/env", {"name": "ANTHROPIC_API_KEY", "value": "sk-test-123456789"})
    r = console.ok("POST", "/api/sessions", {
        "name": "claude-1", "display": "Joseph (Claude)", "provider": "Anthropic", "cwd": proj,
        "command": "bash -c 'echo started in $PWD; echo key=$ANTHROPIC_API_KEY; sleep 300'"})
    w = names(r)["claude-1"]
    assert w["running"] and w["saved"] and w["cwd"] == proj and w["display"] == "Joseph (Claude)"
    text = console.capture("claude-1", needle="key=")
    assert f"started in {proj}" in text, "claude must start in the chosen folder"
    assert "key=sk-test-123456789" in text, "API keys from Settings reach the session env"

    assert console.call("POST", "/api/sessions", {"name": "claude-1", "cwd": proj})[0] == 409
    assert console.call("POST", "/api/sessions", {"name": "x2", "cwd": "/nope/nope"})[0] == 400
    assert console.call("POST", "/api/sessions", {"name": "bad name!", "cwd": proj})[0] == 400

    r = console.ok("POST", "/api/sessions", {"name": "claude-2", "cwd": proj, "command": "sleep 300", "start": False})
    assert names(r)["claude-2"]["running"] is False and names(r)["claude-2"]["status"] == "stopped"

    r = console.ok("PATCH", "/api/sessions/claude-1", {"new_name": "joseph", "display": "Joseph", "autostart": True})
    assert "joseph" in names(r) and "claude-1" not in names(r) and names(r)["joseph"]["autostart"]
    assert "joseph" in console.tmux("list-windows", "-a", "-F", "#{window_name}").split()

    r = console.ok("POST", "/api/sessions/claude-2/start", {"resume": False})
    assert names(r)["claude-2"]["running"]
    r = console.ok("POST", "/api/sessions/joseph/stop")
    assert names(r)["joseph"]["running"] is False and names(r)["joseph"]["saved"]
    assert console.call("POST", "/api/sessions/joseph/stop")[0] == 409
    r = console.ok("POST", "/api/sessions/joseph/start", {"resume": False})
    assert names(r)["joseph"]["running"]

    r = console.ok("DELETE", "/api/sessions/claude-2")
    assert "claude-2" not in names(r)
    assert "claude-2" not in console.tmux("list-windows", "-a", "-F", "#{window_name}").split()

    d = console.ok("GET", "/api/dirs")
    assert proj in d["recent"]


def test_default_command_is_claude(console):
    s = console.ok("GET", "/api/settings")
    assert s["new_window_command"] == "claude"


def test_status_indicator(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "busy", "cwd": proj,
                                         "command": "bash -c 'while :; do echo \"✻ Thinking… (esc to interrupt)\"; sleep 0.3; done'"})
    console.ok("POST", "/api/sessions", {"name": "asks", "cwd": proj,
                                         "command": "bash -c 'printf \"Do you want to proceed?\\n❯ 1. Yes\\n\"; sleep 300'"})
    console.ok("POST", "/api/sessions", {"name": "quiet", "cwd": proj, "command": "bash -c 'echo ready; sleep 300'"})
    deadline = time.time() + 15
    want = {"busy": "working", "asks": "waiting", "quiet": "idle"}
    while time.time() < deadline:
        got = {n: w["status"] for n, w in names(console.ok("GET", "/api/sessions?refresh=1")).items() if n in want}
        if got == want:
            break
        time.sleep(0.5)
    assert got == want


def test_hide_keeps_session_running(console):
    proj = str(console.work / "proj-b")
    console.ok("POST", "/api/sessions", {"name": "waiter", "cwd": proj, "command": "sleep 300"})
    r = console.ok("POST", "/api/sessions/waiter/hide", {"hidden": True})
    w = names(r)["waiter"]
    assert w["hidden"] and w["running"], "hide only removes it from the view"
    assert "waiter" in console.tmux("list-windows", "-a", "-F", "#{window_name}").split()
    r = console.ok("POST", "/api/sessions/waiter/hide", {"hidden": False})
    assert names(r)["waiter"]["hidden"] is False


def test_external_tmux_sessions_are_listed(console):
    console.ok("GET", "/api/sessions")  # make sure the poller runs
    console.tmux("new-session", "-d", "-s", "deneme", "-n", "work", "sleep 300")
    w = console.window("deneme~work")
    assert w["external"] and w["running"]
    r = console.ok("POST", "/api/sessions/deneme~work/hide", {"hidden": True})
    assert names(r)["deneme~work"]["hidden"]
    console.ok("POST", "/api/sessions/deneme~work/stop")
    assert "deneme" not in console.tmux("ls", "-F", "#{session_name}").split()


def test_window_actions(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "act", "cwd": proj, "command": "cat"})
    wid = console.window("act")["id"]
    console.ok("POST", f"/api/windows/{wid}/action", {"action": "send", "text": "hello-$((1+1))", "enter": True})
    assert "hello-$((1+1))" in console.capture("act", needle="hello")
    console.ok("POST", f"/api/windows/{wid}/action", {"action": "interrupt"})
    console.ok("POST", f"/api/windows/{wid}/action", {"action": "respawn", "command": "bash -c 'echo respawned in $PWD; sleep 300'"})
    assert f"respawned in {proj}" in console.capture("act", needle="respawned")
    assert console.call("POST", f"/api/windows/{wid}/action", {"action": "nope"})[0] == 400


def test_messages_board(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "joseph", "cwd": proj, "command": "cat"})
    r = console.ok("POST", "/api/messages", {"from": "user", "to": "joseph", "text": "selam", "deliver": True})
    assert r["delivered"]
    assert "selam" in console.capture("joseph", needle="selam")
    msgs = console.ok("GET", "/api/messages")
    msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
    assert any(m["text"] == "selam" for m in msgs)


def test_devbox_msg_cli_is_public(console):
    import urllib.request
    body = urllib.request.urlopen(console.url + "/devbox-msg").read().decode()
    assert body.startswith("#!") and "post" in body


def test_logout(console):
    assert console.call("POST", "/api/logout")[0] == 200
