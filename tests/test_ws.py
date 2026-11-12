"""Terminal WebSocket: tmux views (grouped sessions), input, resize, scroll (copy-mode) and the plain shell."""
import asyncio
import json

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
