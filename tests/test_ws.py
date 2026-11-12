"""Terminal WebSocket: tmux views (grouped sessions), input, resize, scroll (copy-mode) and the plain shell."""
import asyncio
import json
import re
import time

import aiohttp


async def read_for(ws, secs):
    buf, loop = b"", asyncio.get_running_loop()
    end = loop.time() + secs
    try:
        while True:
            m = await asyncio.wait_for(ws.receive(), max(0.01, end - loop.time()))
            if m.type == aiohttp.WSMsgType.BINARY:
                buf += m.data
            else:
                break
    except asyncio.TimeoutError:
        pass
    return buf.decode(errors="replace")


def test_tmux_view_and_shell(console):
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "ticker", "cwd": proj,
                                         "command": "bash -c 'while :; do echo tick; sleep 0.5; done'"})
    console.ok("POST", "/api/sessions", {"name": "sh", "cwd": proj, "command": "bash"})
    tick, sh = console.window("ticker")["id"], console.window("sh")["id"]
    ws_url = console.url.replace("http", "ws") + "/ws/term"

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(f"{ws_url}?window={tick}&cols=100&rows=30") as ws:
                assert "tick" in await read_for(ws, 2)
                views = [l for l in console.tmux("ls", "-F", "#{session_name}").split() if l.startswith("web-")]
                assert len(views) == 1, "each browser tab gets its own grouped view session"
                await ws.send_str(json.dumps({"t": "select", "w": sh}))
                await ws.send_str(json.dumps({"t": "in", "d": "echo WSOK-$((6*7))\r"}))
                assert "WSOK-42" in await read_for(ws, 2)
                await ws.send_str(json.dumps({"t": "resize", "c": 80, "r": 24}))
                await ws.send_str(json.dumps({"t": "scroll", "n": 3}))
                await asyncio.sleep(0.5)
                assert "1" in console.tmux("list-panes", "-a", "-F", "#{pane_in_mode}").split(), "scroll enters copy-mode"
                await ws.send_str(json.dumps({"t": "scroll", "n": -50}))
            await asyncio.sleep(1)
            assert not [l for l in console.tmux("ls", "-F", "#{session_name}").split() if l.startswith("web-")], \
                "the view session is destroyed when the tab closes"
            async with s.ws_connect(f"{ws_url}?shell=1&cols=100&rows=30") as ws:
                await read_for(ws, 1.5)
                await ws.send_str(json.dumps({"t": "in", "d": "echo SHELL-$((2+3)) [$TMUX]\r"}))
                assert "SHELL-5 []" in await read_for(ws, 2), "shell mode is a plain login shell outside tmux"

    asyncio.run(run())


def test_ws_requires_auth(console):
    async def run():
        async with aiohttp.ClientSession() as s:
            try:
                async with s.ws_connect(console.url.replace("http", "ws") + "/ws/term?shell=1"):
                    return False
            except aiohttp.WSServerHandshakeError as e:
                return e.status == 401
    assert asyncio.run(run())


def test_restart_tells_terminals_and_keeps_sessions(console):
    """Deploy/restart: open terminals are closed with 1012 (service restart) so the browser reconnects at
    once; the tmux session itself keeps running."""
    console.ok("POST", "/api/sessions", {"name": "keep", "cwd": str(console.work / "proj-a"), "command": "cat"})
    wid = console.window("keep")["id"]
    ws_url = console.url.replace("http", "ws") + f"/ws/term?window={wid}&cols=80&rows=24"

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(ws_url) as ws:
                await read_for(ws, 1)
                console.proc.terminate()  # what `docker compose up` does to the old container (SIGTERM)
                while True:
                    m = await asyncio.wait_for(ws.receive(), 10)
                    if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        return ws.close_code
    code = asyncio.run(run())
    console.proc.wait(10)
    assert code == 1012
    assert "keep" in console.tmux("list-windows", "-a", "-F", "#{window_name}").split(), "claude keeps running"


def test_typing_leaves_copy_mode_from_another_device(console):
    """The phone scrolled up (tmux copy-mode, which belongs to the pane) and was left like that: typing on the
    desktop must reach the program instead of copy-mode (that used to need Stop + Resume)."""
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "cm", "cwd": proj, "command": "bash"})
    wid = console.window("cm")["id"]
    url = console.url.replace("http", "ws") + f"/ws/term?window={wid}"
    in_mode = lambda: console.tmux("display", "-p", "-t", wid, "#{pane_in_mode}").strip()

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(url + "&cols=51&rows=30") as phone, s.ws_connect(url + "&cols=200&rows=50") as desk:
                await desk.send_str(json.dumps({"t": "in", "d": "seq 1 300\r"}))
                await read_for(desk, 1.5)
                await phone.send_str(json.dumps({"t": "scroll", "n": 20}))
                await asyncio.sleep(0.7)
                assert in_mode() == "1", "phone scrolled: pane in copy-mode"
                await desk.send_str(json.dumps({"t": "in", "d": "echo TYPED-$((6*7))\r"}))
                await asyncio.sleep(1.0)
                assert in_mode() == "0", "typing on the desktop left copy-mode"
    asyncio.run(run())
    assert "TYPED-42" in console.capture("cm", needle="TYPED-42"), "the keys reached the shell"


def test_own_claims_keep_scrolling(console):
    """Touching the phone's screen claims the window size (focus-in). That must not end the phone's own
    scrolling (every new swipe used to jump back to the bottom); a claim from another device does end it."""
    console.ok("POST", "/api/sessions", {"name": "sc", "cwd": str(console.work / "proj-a"), "command": "bash"})
    wid = console.window("sc")["id"]
    url = console.url.replace("http", "ws") + f"/ws/term?window={wid}"
    in_mode = lambda: console.tmux("display", "-p", "-t", wid, "#{pane_in_mode}").strip()

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(url + "&cols=51&rows=30") as phone, s.ws_connect(url + "&cols=200&rows=50") as desk:
                await desk.send_str(json.dumps({"t": "in", "d": "seq 1 400\r"}))
                await read_for(desk, 1.5)
                for _ in range(3):  # swipe, lift, touch again, swipe…
                    await phone.send_str(json.dumps({"t": "claim"}))
                    await phone.send_str(json.dumps({"t": "scroll", "n": 10}))
                    await asyncio.sleep(0.4)
                    assert in_mode() == "1", "the phone's own touches keep it scrolled"
                pos = console.tmux("display", "-p", "-t", wid, "#{scroll_position}").strip()
                assert int(pos or 0) >= 25, f"scrolling adds up across swipes: {pos}"
                await desk.send_str(json.dumps({"t": "claim"}))
                await asyncio.sleep(0.6)
                assert in_mode() == "0", "the desktop taking the screen ends the phone's scrolling"
    asyncio.run(run())


def test_scroll_full_screen_program_gets_wheel_events(console):
    """claude draws on the alternate screen (no tmux history: copy-mode showed "[0/0]" and nothing moved)
    and asks for mouse input: swipes and the wheel must reach it as wheel events, not copy-mode."""
    import sys
    from pathlib import Path
    fake = Path(__file__).parent / "fakebin" / "fullscreen"
    proj = console.work / "proj-a"
    for name, extra in (("fs-sgr", ""), ("fs-x10", " --no-sgr")):
        log = proj / f"{name}.log"
        console.ok("POST", "/api/sessions", {"name": name, "cwd": str(proj), "command": f"{sys.executable} {fake} {log}{extra}"})
        console.capture(name, needle="FULLSCREEN READY")
        wid = console.window(name)["id"]

        async def run():
            async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
                async with s.ws_connect(console.url.replace("http", "ws") + f"/ws/term?window={wid}&cols=51&rows=30") as ws:
                    await read_for(ws, 0.5)
                    await ws.send_str(json.dumps({"t": "scroll", "n": 3}))   # finger down = content up
                    await ws.send_str(json.dumps({"t": "scroll", "n": -2}))
                    await asyncio.sleep(1)
        asyncio.run(run())
        got = log.read_text() if log.exists() else ""
        assert console.tmux("display", "-p", "-t", wid, "#{pane_in_mode}").strip() == "0", "no copy-mode"
        if not extra:
            assert got.count("\\x1b[<64;") == 3 and got.count("\\x1b[<65;") == 2, got
        else:
            assert got.count("\\x1b[M`") == 3 and got.count("\\x1b[Ma") == 2, got  # 32+64 / 32+65


def test_scroll_plain_output_uses_copy_mode(console):
    console.ok("POST", "/api/sessions", {"name": "plain", "cwd": str(console.work / "proj-a"), "command": "bash"})
    wid = console.window("plain")["id"]

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(console.url.replace("http", "ws") + f"/ws/term?window={wid}&cols=80&rows=24") as ws:
                await ws.send_str(json.dumps({"t": "in", "d": "seq 1 300\r"}))
                await read_for(ws, 1.5)
                await ws.send_str(json.dumps({"t": "scroll", "n": 15}))
                await asyncio.sleep(0.6)
    asyncio.run(run())
    assert console.tmux("display", "-p", "-t", wid, "#{pane_in_mode} #{scroll_position}").split() == ["1", "15"]


# ---------------------------------------------------------------- buffer terminal (tmux control mode)

async def collect(ws, secs, until=None):
    """(text frames, bytes) received for `secs` seconds, or until `until` appears in the bytes"""
    texts, buf, loop = [], b"", asyncio.get_running_loop()
    end = loop.time() + secs
    try:
        while True:
            m = await asyncio.wait_for(ws.receive(), max(0.01, end - loop.time()))
            if m.type == aiohttp.WSMsgType.BINARY:
                buf += m.data
                if until and until in buf:
                    break
            elif m.type == aiohttp.WSMsgType.TEXT:
                texts.append(m.data)
            else:
                break
    except asyncio.TimeoutError:
        pass
    return texts, buf


def test_buffer_terminal(console):
    """The browser keeps the scrollback: raw output byte for byte (nothing dropped when it is fast), history
    replayed on connect, exact input, sizes from the server."""
    proj = str(console.work / "proj-a")
    console.ok("POST", "/api/sessions", {"name": "buf", "cwd": proj, "command": "bash --norc"})
    console.ok("POST", "/api/sessions", {"name": "buf2", "cwd": proj, "command": "bash --norc"})
    wid, wid2 = console.window("buf")["id"], console.window("buf2")["id"]
    console.ok("POST", f"/api/windows/{wid}/action", {"action": "send", "text": "seq 1 300", "enter": True})
    time.sleep(1)
    base = console.url.replace("http", "ws") + f"/ws/term?window={wid}&mode=buffer&lines=5000"
    size = lambda w: console.tmux("display", "-p", "-t", w, "#{window_width}x#{window_height}").strip()

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(base + "&cols=90&rows=30") as ws:
                texts, buf = await collect(ws, 3, until=b"\x1b[?2004h")
                assert "size:90x30" in texts and size(wid) == "90x30", "the device that opens the window sizes it"
                assert b"\r\n150\r\n" in buf and b"300" in buf, "history before the connection is replayed"

                # (wait for a computed marker: the echoed command line itself contains "3000")
                await ws.send_str(json.dumps({"t": "in", "d": "seq 1000 3000; echo END-$((40+2))\r"}))
                _, buf = await collect(ws, 8, until=b"END-42")
                got = set(int(x) for x in re.findall(rb"(?<![\d;?\[])(\d{4})(?=\r\n)", buf))
                missing = [n for n in range(1000, 3001) if n not in got]
                assert not missing, f"fast output arrives completely: missing {missing[:10]} ({len(missing)}) near {buf[buf.find(str(missing[0]-1).encode())-40:][:120] if missing else ''}"

                await ws.send_str(json.dumps({"t": "in", "d": "echo 'şğü-'$((6*7))\r"}))
                _, buf = await collect(ws, 3, until="şğü-42".encode())
                assert "şğü-42".encode() in buf, "exact input, UTF-8 too"

                await ws.send_str(json.dumps({"t": "resize", "c": 60, "r": 20}))
                texts, _ = await collect(ws, 1.5)
                assert size(wid) == "60x20" and "size:60x20" in texts

                async with s.ws_connect(base + "&cols=50&rows=25") as phone:
                    await collect(phone, 2)
                    texts, _ = await collect(ws, 1.5)
                    assert size(wid) == "50x25" and "size:50x25" in texts, "the other device follows the new size"
                    await ws.send_str(json.dumps({"t": "claim", "c": 60, "r": 20}))
                    await asyncio.sleep(1)
                    assert size(wid) == "60x20", "claiming takes it back"

                await ws.send_str(json.dumps({"t": "select", "w": wid2}))
                texts, buf = await collect(ws, 2)
                assert b"\x1bc" in buf and any(t.startswith("size:") for t in texts), "other window replayed"
    asyncio.run(run())


def test_buffer_terminal_full_screen_program(console):
    """A full-screen program is replayed on the alternate screen with its mouse modes; scrolling sends it
    wheel events (it has no scrollback)."""
    import sys
    from pathlib import Path
    fake = Path(__file__).parent / "fakebin" / "fullscreen"
    proj = console.work / "proj-a"
    log = proj / "fsb.log"
    console.ok("POST", "/api/sessions", {"name": "fsb", "cwd": str(proj), "command": f"{sys.executable} {fake} {log}"})
    console.capture("fsb", needle="FULLSCREEN READY")
    wid = console.window("fsb")["id"]

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(console.url.replace("http", "ws") + f"/ws/term?window={wid}&mode=buffer&cols=70&rows=20") as ws:
                _, buf = await collect(ws, 3, until=b"\x1b[?1006h")
                assert b"\x1b[?1049h" in buf and b"FULLSCREEN READY" in buf and b"\x1b[?1003h" in buf
                await ws.send_str(json.dumps({"t": "scroll", "n": 2}))
                await asyncio.sleep(1)
    asyncio.run(run())
    assert log.read_text().count("\\x1b[<64;") == 2
