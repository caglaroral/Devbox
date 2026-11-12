"""Claude conversations per session: pinned ids, the SessionStart hook, Resume/Fork/Delete, the history list."""
import json
import re
import time

ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def rec(console, name, host="local"):
    path = console.data / "users" / console.user / "boxes" / host / "sessions.json"
    return json.loads(path.read_text())["sessions"][name]


def ran_id(console, name, needle="FAKECLAUDE id=", host="local"):
    text = console.capture(name, host=host, needle=needle)
    ids = re.findall(rf"FAKECLAUDE id=({ID})", text)
    assert ids, text
    return ids[-1], text


def stop(console, name):
    console.ok("POST", f"/api/sessions/{name}/stop")


def start(console, name, resume=True):
    console.ok("POST", f"/api/sessions/{name}/start", {"resume": resume})


def send(console, name, text):
    wid = console.window(name)["id"]
    console.ok("POST", f"/api/windows/{wid}/action", {"action": "send", "text": text, "enter": True})


def test_each_session_keeps_its_own_conversation(console):
    proj = str(console.work / "proj-a")
    for n in ("alice", "bob"):  # two sessions in the SAME folder: --continue would mix them up
        console.ok("POST", "/api/sessions", {"name": n, "cwd": proj, "command": console.claude})
    a, text = ran_id(console, "alice")
    assert f"--session-id {a}" in text and rec(console, "alice")["claude_id"] == a
    b, _ = ran_id(console, "bob")
    assert a != b

    for n in ("alice", "bob"):
        stop(console, n)
    start(console, "bob")      # bob was used last; alice must still get her own conversation
    start(console, "alice")
    a2, text = ran_id(console, "alice", needle="--resume")
    assert a2 == a and f"--resume {a}" in text
    assert ran_id(console, "bob", needle="--resume")[0] == b

    # Restart = new conversation; Resume = the session's own one
    console.ok("POST", "/api/sessions/alice/restart", {"resume": False})
    time.sleep(1)
    a3, text = ran_id(console, "alice", needle="--session-id")
    assert a3 != a and rec(console, "alice")["claude_id"] == a3


def test_hook_follows_clear(console):
    console.ok("POST", "/api/sessions", {"name": "c1", "cwd": str(console.work / "proj-b"), "command": console.claude})
    first, _ = ran_id(console, "c1")
    send(console, "c1", "/clear")
    deadline = time.time() + 10
    while True:  # claude switched conversation inside the session: the hook reported it
        cur = console.ok("GET", "/api/sessions/c1/conversations")["current"]
        if cur != first or time.time() > deadline:
            break
        time.sleep(0.3)
    assert cur != first and re.fullmatch(ID, cur)
    stop(console, "c1")
    start(console, "c1")
    assert ran_id(console, "c1", needle="--resume")[0] == cur, "Resume opens the conversation after /clear"


def test_history_resume_fork_delete(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "h", "cwd": proj, "command": console.claude})
    one, _ = ran_id(console, "h")
    console.ok("POST", "/api/sessions/h/restart", {"resume": False})
    time.sleep(1)
    two, _ = ran_id(console, "h", needle="--session-id")
    assert two != one

    r = console.ok("GET", "/api/sessions/h/conversations")
    ids = [c["id"] for c in r["conversations"]]
    assert set(ids) >= {one, two} and ids[0] == two, "newest first"
    assert r["current"] == two and r["running"] and r["claude"]
    assert {c["id"]: c for c in r["conversations"]}[one]["prompt"] == f"hello from {one[:8]}"

    # the running conversation cannot be deleted; switching back to the first one works in place
    assert console.call("POST", f"/api/sessions/h/conversations/{two}", {"action": "delete"})[0] == 409
    console.ok("POST", f"/api/sessions/h/conversations/{one}", {"action": "resume"})
    time.sleep(1)
    assert ran_id(console, "h", needle=f"--resume {one}")[0] == one
    assert console.ok("GET", "/api/sessions/h/conversations")["current"] == one

    # fork: continue `two` as a new conversation, the original stays
    console.ok("POST", f"/api/sessions/h/conversations/{two}", {"action": "fork"})
    time.sleep(1)
    forked, text = ran_id(console, "h", needle="--fork-session")
    assert forked not in (one, two) and f"--resume {two} --fork-session" in text
    deadline = time.time() + 10
    while console.ok("GET", "/api/sessions/h/conversations")["current"] != forked and time.time() < deadline:
        time.sleep(0.3)
    assert console.ok("GET", "/api/sessions/h/conversations")["current"] == forked

    console.ok("POST", f"/api/sessions/h/conversations/{two}", {"action": "delete"})
    assert two not in [c["id"] for c in console.ok("GET", "/api/sessions/h/conversations")["conversations"]]
    assert console.call("POST", "/api/sessions/h/conversations/not-an-id", {"action": "delete"})[0] == 400
    assert console.call("POST", f"/api/sessions/h/conversations/{two}", {"action": "resume"})[0] == 404


def test_custom_commands_are_left_alone(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "own", "cwd": proj, "command": f"{console.claude} --continue"})
    _, text = ran_id(console, "own")
    assert "--session-id" not in text and "--settings" not in text, "a command with its own flags is not touched"
    r = console.ok("GET", "/api/sessions/own/conversations")
    assert r["claude"] is False


def test_remote_conversations(console):
    console.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    (console.remote_home / "rp").mkdir()
    console.ok("POST", "/api/sessions?host=gpu-box", {"name": "rc", "cwd": "~/rp", "command": console.claude})
    cid, _ = ran_id(console, "rc", host="gpu-box")
    assert (console.remote_home / ".claude/projects").exists()
    r = console.ok("GET", "/api/sessions/rc/conversations?host=gpu-box")
    assert r["current"] == cid and [c["id"] for c in r["conversations"]] == [cid]


def test_claude_in_local_bin_is_found(console):
    """Claude Code's installer puts claude in ~/.local/bin, which a login PATH often lacks (root on Debian):
    sessions and the host probe must still find it."""
    import shutil
    console.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    lb = console.remote_home / ".local" / "bin"
    lb.mkdir(parents=True)
    shutil.copy(console.claude, lb / "claude")
    (console.remote_home / "lp").mkdir()
    console.ok("POST", "/api/sessions?host=gpu-box", {"name": "plain", "cwd": "~/lp", "command": "claude"})
    cid, text = ran_id(console, "plain", host="gpu-box")
    assert f"--session-id {cid}" in text
    info = console.ok("POST", "/api/hosts/gpu-box/test")
    assert info.get("claude"), f"probe finds ~/.local/bin/claude: {info}"


def test_failing_command_stays_visible(console):
    """A command that fails at once (e.g. "claude: command not found") must leave a dead window with its
    error, not vanish (it used to take the window, and as the only one the tmux server, with it)."""
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "broken", "cwd": proj, "command": "sh -c 'echo no-such-thing; exit 127'"})
    deadline = time.time() + 10
    while True:
        w = console.window("broken")
        if w["status"] == "dead" or time.time() > deadline:
            break
        time.sleep(0.3)
    assert w["running"] and w["status"] == "dead"
    assert "no-such-thing" in console.capture("broken", needle="no-such-thing")
