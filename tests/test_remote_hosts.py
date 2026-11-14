"""Remote hosts over (fake) SSH: host CRUD, password -> key install, remote sessions, messages, shell."""
import asyncio
import json

import aiohttp


def short(hosts):
    return {h["id"]: h for h in hosts}


def test_host_validation(console):
    st, r = console.call("POST", "/api/hosts", {"label": "x", "hostname": "nohost", "username": "dev", "password": "secret"})
    assert st >= 400 and "refused" in r
    st, r = console.call("POST", "/api/hosts", {"label": "Fake", "hostname": "fakehost", "username": "dev", "password": "nope"})
    assert st >= 400 and "rejected" in r


def test_remote_flow(console):
    r = console.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    h = short(r["hosts"])["gpu-box"]
    assert h["remote"] and not h["error"] and h["auth"] == "console-key"
    ak = (console.remote_home / ".ssh/authorized_keys").read_text()
    assert "ssh-ed25519" in ak, "the password is used once to install the console key"
    assert console.call("POST", "/api/hosts", {"label": "Dup", "hostname": "fakehost", "username": "dev",
                                               "password": "secret"})[0] == 409

    (console.remote_home / "proj-a").mkdir()
    r = console.ok("POST", "/api/sessions?host=gpu-box", {
        "name": "r1", "display": "Remote one", "cwd": "~/proj-a",
        "command": "bash -c 'echo remote in $PWD home=$HOME; sleep 300'"})
    w = {x["name"]: x for x in r["windows"]}["r1"]
    assert w["running"] and w["cwd"] == str(console.remote_home / "proj-a")
    assert "r1" not in [x["name"] for x in console.ok("GET", "/api/sessions?host=local")["windows"]]
    text = console.capture("r1", host="gpu-box", needle="remote in")
    assert f"remote in {console.remote_home}/proj-a home={console.remote_home}" in text
    assert "r1" in console.tmux("list-windows", "-a", "-F", "#{window_name}", sock="remote").split()

    assert any(d.endswith("proj-a") for d in console.ok("GET", "/api/dirs?host=gpu-box")["dirs"])
    console.ok("PATCH", "/api/sessions/r1?host=gpu-box", {"new_name": "r-one"})
    r = console.ok("POST", "/api/messages", {"from": "user", "to": "r-one@gpu-box", "text": "hi remote", "deliver": True})
    assert r["delivered"]
    t = console.ok("POST", "/api/hosts/gpu-box/test")
    assert t.get("ok", True)
    r = console.ok("PATCH", "/api/hosts/gpu-box", {"label": "GPU"})
    assert short(r["hosts"])["gpu-box"]["label"] == "GPU"

    wid = console.window("r-one", host="gpu-box")["id"]
    ws_url = console.url.replace("http", "ws") + "/ws/term?host=gpu-box"

    async def run():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(f"{ws_url}&window={wid}&cols=100&rows=30") as ws:
                await asyncio.sleep(1)
                buf = b""
                try:
                    while True:
                        m = await asyncio.wait_for(ws.receive(), 1.5)
                        if m.type != aiohttp.WSMsgType.BINARY:
                            break
                        buf += m.data
                except asyncio.TimeoutError:
                    pass
                assert len(buf) > 100, "remote tmux view streams the screen"
            async with s.ws_connect(f"{ws_url}&shell=1&cols=100&rows=30") as ws:
                await asyncio.sleep(1)
                await ws.send_str(json.dumps({"t": "in", "d": "echo RSH-$((6*7)) $HOME\r"}))
                buf = b""
                try:
                    while True:
                        m = await asyncio.wait_for(ws.receive(), 2)
                        if m.type != aiohttp.WSMsgType.BINARY:
                            break
                        buf += m.data
                except asyncio.TimeoutError:
                    pass
                t = buf.decode(errors="replace")
                assert "RSH-42" in t and str(console.remote_home) in t, "SSH shell into the remote host"

    asyncio.run(run())

    console.ok("DELETE", "/api/sessions/r-one?host=gpu-box")
    r = console.ok("DELETE", "/api/hosts/gpu-box")
    assert "gpu-box" not in short(r["hosts"])


def test_pasted_private_key_and_manual(console):
    st, r = console.call("POST", "/api/hosts", {"label": "Fake", "hostname": "fakehost", "username": "dev", "auth": "manual"})
    # manual = "I installed the console key myself"; without it the probe fails but the host is saved with an error
    if st == 200:
        assert short(r["hosts"])["fake"]["error"]
        console.ok("DELETE", "/api/hosts/fake")


def test_askpass_is_runnable_even_if_runtime_is_noexec(console):
    """Docker mounts the tmpfs runtime dir noexec; the askpass helper must live somewhere executable."""
    import os
    import subprocess
    askpass = console.data / "askpass.sh"
    assert askpass.exists() and os.access(askpass, os.X_OK)
    out = subprocess.run([str(askpass)], env={"DEVBOX_ASKPASS_PW": "p@ss"}, capture_output=True, text=True).stdout
    assert out == "p@ss\n"


def test_ask_password_every_connection(console):
    """"ask" hosts: no key installed, nothing stored; the password opens one master connection and is forgotten."""
    r = console.ok("POST", "/api/hosts", {"label": "Later", "hostname": "fakehost", "username": "later", "auth": "ask"})
    assert short(r["hosts"])["later"]["locked"], "added without a password: locked until it is entered"
    r = console.ok("POST", "/api/hosts/later/unlock", {"password": "secret"})
    assert not short(r["hosts"])["later"]["locked"]
    console.ok("DELETE", "/api/hosts/later")
    st, r = console.call("POST", "/api/hosts", {"label": "Ask Box", "hostname": "fakehost", "username": "dev",
                                                "auth": "ask", "password": "nope"})
    assert st >= 400 and "wrong password" in r
    r = console.ok("POST", "/api/hosts", {"label": "Ask Box", "hostname": "fakehost", "username": "dev",
                                          "auth": "ask", "password": "secret"})
    h = short(r["hosts"])["ask-box"]
    assert h["auth"] == "ask" and not h["locked"] and not h["error"]
    ak = console.remote_home / ".ssh/authorized_keys"
    assert not ak.exists() or "devbox-console" not in ak.read_text(), "no key is installed"
    assert "secret" not in json.dumps(json.loads((console.data / "users").glob("*/hosts.json").__next__().read_text()))
    (console.remote_home / "proj-q").mkdir(exist_ok=True)
    console.ok("POST", "/api/sessions?host=ask-box", {"name": "q1", "cwd": "~/proj-q", "command": "sleep 300"})
    assert console.window("q1", host="ask-box")["running"]

    r = console.ok("POST", "/api/hosts/ask-box/lock")
    assert short(r["hosts"])["ask-box"]["locked"]
    st, r = console.call("GET", "/api/fs/list?host=ask-box&path=~")
    assert st >= 400 and "locked" in r, "nothing reaches a locked host"
    assert console.call("POST", "/api/hosts/ask-box/unlock", {"password": "nope"})[0] >= 400
    r = console.ok("POST", "/api/hosts/ask-box/unlock", {"password": "secret"})
    assert not short(r["hosts"])["ask-box"]["locked"]
    console.ok("DELETE", "/api/sessions/q1?host=ask-box")

    # a terminal keeps the connection; when the last one closes the host locks, the next terminal asks again
    ws_url = console.url.replace("http", "ws") + "/ws/term?host=ask-box&shell=1&cols=80&rows=24"

    async def term():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(ws_url) as ws:
                await ws.send_str(json.dumps({"t": "in", "d": "echo ASK-$((6*7))\r"}))
                buf = b""
                try:
                    while b"ASK-42" not in buf:
                        m = await asyncio.wait_for(ws.receive(), 3)
                        if m.type != aiohttp.WSMsgType.BINARY:
                            break
                        buf += m.data
                except asyncio.TimeoutError:
                    pass
                assert b"ASK-42" in buf
            await asyncio.sleep(4.5)
            async with s.ws_connect(ws_url) as ws:
                m = await asyncio.wait_for(ws.receive(), 3)
                assert m.type == aiohttp.WSMsgType.TEXT and m.data == "locked"

    asyncio.run(term())
    assert short(console.ok("GET", "/api/hosts")["hosts"])["ask-box"]["locked"]
    console.ok("POST", "/api/hosts/ask-box/unlock", {"password": "secret"})

    # switching an existing key host to "ask" can take the console key out of authorized_keys
    console.ok("DELETE", "/api/hosts/ask-box")
    console.ok("POST", "/api/hosts", {"label": "Key Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    assert "devbox-console" in ak.read_text()
    r = console.ok("PATCH", "/api/hosts/key-box", {"auth": "ask", "password": "secret", "remove_key": True})
    assert short(r["hosts"])["key-box"]["auth"] == "ask"
    assert "devbox-console" not in ak.read_text(), "console key removed from the host"
    r = console.ok("PATCH", "/api/hosts/key-box", {"label": "Key Box 2"})
    assert short(r["hosts"])["key-box"]["auth"] == "ask", "editing other fields keeps the mode"
    console.ok("DELETE", "/api/hosts/key-box")


def test_host_order(console):
    console.ok("POST", "/api/hosts", {"label": "Box A", "hostname": "fakehost", "username": "dev", "password": "secret"})
    console.ok("POST", "/api/hosts", {"label": "Box B", "hostname": "fakehost", "username": "dev2", "password": "secret"})
    ids = [h["id"] for h in console.ok("GET", "/api/hosts")["hosts"]]
    assert ids == ["local", "box-a", "box-b"]
    r = console.ok("PUT", "/api/hosts/order", {"order": ["box-b", "local", "nope", "box-a"]})
    assert [h["id"] for h in r["hosts"]] == ["box-b", "local", "box-a"], "unknown ids are dropped"
    console.ok("POST", "/api/hosts", {"label": "Box C", "hostname": "fakehost", "username": "dev3", "password": "secret"})
    assert [h["id"] for h in console.ok("GET", "/api/hosts")["hosts"]] == ["box-b", "local", "box-a", "box-c"], \
        "new hosts go to the end"
    settings = (console.data / "users" / console.user / "settings.json").read_text()
    assert "new_window_command" not in settings, "only the order is written"


def test_ask_host_stays_open_while_a_page_is_in_view(console):
    import time
    console.ok("POST", "/api/hosts", {"label": "Held", "hostname": "fakehost", "username": "held", "auth": "ask",
                                      "password": "secret"})
    console.ok("POST", "/api/hosts/hold", {"page": "p1", "hosts": ["held"]})
    ws_url = console.url.replace("http", "ws") + "/ws/term?host=held&shell=1&cols=80&rows=24"

    async def open_close():
        async with aiohttp.ClientSession(headers={"Cookie": console.cookie}) as s:
            async with s.ws_connect(ws_url) as ws:
                await asyncio.sleep(0.5)

    asyncio.run(open_close())
    time.sleep(4.5)
    assert not short(console.ok("GET", "/api/hosts")["hosts"])["held"]["locked"], "page in view: still connected"
    console.ok("POST", "/api/hosts/hold", {"page": "p1", "release": True})
    deadline = time.time() + 8
    while time.time() < deadline and not short(console.ok("GET", "/api/hosts")["hosts"])["held"]["locked"]:
        time.sleep(0.5)
    assert short(console.ok("GET", "/api/hosts")["hosts"])["held"]["locked"], "tab hidden / closed: connection closes"
    console.ok("DELETE", "/api/hosts/held")
