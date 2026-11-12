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
