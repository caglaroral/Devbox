"""Per-user event log (Settings → Event log): what gets logged, ping/pong, 7-day pruning, clearing."""
import asyncio
import json
import os
import time

import aiohttp


def log_lines(console):
    return console.ok("GET", "/api/logs?lines=1000")["lines"]


def wait_for(console, needle, timeout=10):
    deadline = time.time() + timeout
    while True:
        lines = log_lines(console)
        if any(needle in l for l in lines) or time.time() > deadline:
            return lines
        time.sleep(0.3)


def test_terminal_and_session_events(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "logme", "cwd": proj, "command": "cat"})
    wid = console.window("logme")["id"]
    ws_url = console.url.replace("http", "ws") + f"/ws/term?window={wid}&cols=90&rows=30"

    async def run():
        hdr = {"Cookie": console.cookie, "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile"}
        async with aiohttp.ClientSession(headers=hdr) as s:
            async with s.ws_connect(ws_url) as ws:
                await ws.send_str(json.dumps({"t": "ping"}))
                deadline = asyncio.get_running_loop().time() + 5
                while True:  # skip terminal output until the pong arrives
                    m = await asyncio.wait_for(ws.receive(), deadline - asyncio.get_running_loop().time())
                    if m.type == aiohttp.WSMsgType.TEXT:
                        assert m.data == "pong"
                        break
    asyncio.run(run())

    lines = wait_for(console, "term.close")
    opened = [l for l in lines if " term.open " in l]
    closed = [l for l in lines if " term.close " in l]
    assert opened and f"target={wid}" in opened[-1] and "device=mobile" in opened[-1] and "size=90x30" in opened[-1]
    assert closed and "by=browser" in closed[-1] and "secs=" in closed[-1]
    assert any(" session.create " in l and "name=logme" in l for l in lines)

    console.ok("POST", "/api/sessions/logme/stop")
    console.ok("DELETE", "/api/sessions/logme")
    lines = log_lines(console)
    assert any(" session.stop " in l for l in lines) and any(" session.delete " in l for l in lines)


def test_prune_and_clear(console):
    console.ok("POST", "/api/sessions", {"name": "x", "cwd": str(console.work / "proj-a"), "command": "cat", "start": False})
    logdir = console.data / "users" / console.user / "logs"
    assert list(logdir.glob("*.log")), "events are written under <data>/users/<name>/logs"
    old = logdir / "20000101.log"
    old.write_text("2000-01-01 00:00:00 ancient\n")
    os.utime(old, (time.time() - 8 * 86400,) * 2)

    # a new day file triggers pruning; simulate by restarting the console on the same data dir
    console.stop()
    from conftest import Console
    c2 = Console(console.base)
    try:
        c2.ok("GET", "/api/logs")
        c2.ok("POST", "/api/sessions", {"name": "y", "cwd": str(c2.work / "proj-a"), "command": "cat", "start": False})
        assert not old.exists(), "log files older than 7 days are deleted"
        r = c2.ok("GET", "/api/logs")
        assert r["days"] == 7 and r["files"] >= 1 and r["lines"]
        assert c2.ok("DELETE", "/api/logs")["deleted"] >= 1
        assert c2.ok("GET", "/api/logs")["lines"] == []
    finally:
        c2.stop()
