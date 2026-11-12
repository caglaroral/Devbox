"""Shared fixtures: start a real devbox console on a free port with an isolated data dir, isolated tmux
servers (a tmux wrapper with its own socket names) and a fake `ssh` for remote hosts.

The app has no test backdoor: the session cookie is signed with the test data dir's own `secret`."""
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FAKEBIN = Path(__file__).resolve().parent / "fakebin"
REAL_TMUX = shutil.which("tmux")

if not REAL_TMUX:
    pytest.exit("tmux is required for the tests", returncode=3)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Console:
    def __init__(self, base: Path, extra_env: dict | None = None):
        self.base = base
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.data = base / "data"
        self.remote_home = base / "remotehome"
        self.work = base / "work"  # folders sessions start in
        for d in (self.remote_home, self.work / "proj-a", self.work / "proj-b"):
            d.mkdir(parents=True, exist_ok=True)
        self.tmux_label = f"devboxtest-{os.getpid()}-{self.port}"
        bindir = base / "bin"
        bindir.mkdir(exist_ok=True)
        # The app rebuilds the env of local processes (only PATH survives), so the socket label is baked in.
        (bindir / "tmux").write_text(f'#!/bin/sh\nexec {REAL_TMUX} -L "{self.tmux_label}-${{TMUX_SOCK:-local}}" "$@"\n')
        (bindir / "tmux").chmod(0o755)
        shutil.copy(FAKEBIN / "ssh", bindir / "ssh")
        shutil.copy(FAKEBIN / "claude", bindir / "claude")
        self.claude = str(bindir / "claude")  # by full path: login shells reset PATH
        self.path = f"{bindir}:{os.environ.get('PATH', '')}"
        env = {**os.environ, "PATH": self.path, "DEVBOX_PORT": str(self.port), "DEVBOX_DATA": str(self.data),
               "DEVBOX_RUNTIME": str(base / "run"), "DEVBOX_POLL": "1", "DEVBOX_AUTH": "pam",
               "DEVBOX_LOCAL_LABEL": "testbox", "FAKE_REMOTE_HOME": str(self.remote_home),
               "PYTHONPATH": str(SRC)}
        for k in ("TMUX", "TMUX_PANE", "VAULT_ADDR", "VAULT_TOKEN"):
            env.pop(k, None)
        env.update(extra_env or {})
        self.log = open(base / "server.log", "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "devbox_console.app"], cwd=SRC, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + (base / "server.log").read_text())
            try:
                urllib.request.urlopen(self.url + "/api/whoami", timeout=1).read()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not start:\n" + (base / "server.log").read_text())
        self.user = os.environ.get("USER") or __import__("pwd").getpwuid(os.getuid()).pw_name
        secret = (self.data / "secret").read_text().strip().encode()
        val = f"{self.user}|{time.time() + 3600}"
        self.cookie_value = val + "." + hmac.new(secret, val.encode(), hashlib.sha256).hexdigest()
        self.cookie = "devbox_auth=" + self.cookie_value

    def call(self, method: str, path: str, body=None, auth: bool = True, timeout: float = 60):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.url + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def ok(self, method: str, path: str, body=None):
        st, r = self.call(method, path, body)
        assert st == 200, f"{method} {path} -> {st}: {r}"
        return r

    def tmux(self, *args: str, sock: str = "local") -> str:
        return subprocess.run([REAL_TMUX, "-L", f"{self.tmux_label}-{sock}", *args],
                              capture_output=True, text=True).stdout

    def window(self, name: str, host: str = "local", timeout: float = 10) -> dict:
        deadline = time.time() + timeout
        while True:
            snap = self.ok("GET", f"/api/sessions?host={host}&refresh=1")
            w = next((w for w in snap["windows"] if w["name"] == name), None)
            if w or time.time() > deadline:
                assert w, f"window {name} not found on {host}: {[x['name'] for x in snap['windows']]}"
                return w
            time.sleep(0.3)

    def capture(self, name: str, host: str = "local", needle: str = "", timeout: float = 10) -> str:
        deadline = time.time() + timeout
        while True:
            wid = self.window(name, host)["id"]
            text = self.ok("GET", f"/api/windows/{wid}/capture?host={host}&lines=50")["text"]
            if needle in text or time.time() > deadline:
                return text
            time.sleep(0.3)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        for sock in ("local", "remote"):
            subprocess.run([REAL_TMUX, "-L", f"{self.tmux_label}-{sock}", "kill-server"], capture_output=True)


@pytest.fixture
def make_console(tmp_path_factory):
    started = []

    def make(extra_env: dict | None = None, base: Path | None = None) -> Console:
        c = Console(base or tmp_path_factory.mktemp("console"), extra_env)
        started.append(c)
        return c

    yield make
    for c in started:
        c.stop()


@pytest.fixture
def console(make_console) -> Console:
    return make_console()
