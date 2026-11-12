"""devbox console — manage tmux + Claude CLI sessions on N machines from one web page.

One process, no Caddy, no ttyd:
  browser (xterm.js) ──WebSocket──► app.py ──PTY──► tmux new-session -t main          (this machine)
                                        └──────PTY──► ssh -tt host tmux new-session …   (remote hosts)

Login = this machine's own Linux users (like Proxmox' "Linux PAM standard authentication"):
  DEVBOX_AUTH=pam (bare metal): PAM in-process; local processes run AS THAT USER (setuid when root;
                   as a normal user only that same user can log in).
  DEVBOX_AUTH=ssh (Docker):     the container checks the credentials by SSHing to the host
                   (DEVBOX_LOCAL_SSH, the host's sshd runs PAM) and then manages "this machine"
                   over SSH as that user, exactly like a remote host.

Secrets (API keys, SSH private keys, optional stored host passwords) live in HashiCorp Vault KV v2
when VAULT_ADDR is set (see secretstore.py), otherwise in 0600 files. Private keys are only written
to a tmpfs runtime dir for ssh -i.

Hosts (Termius-like): every console user has "local" plus any number of remote machines reached over
SSH. Adding a host uses its password once to install the user's console key (ed25519) into the
remote authorized_keys; the password is never stored. Remote calls share one ControlMaster connection.

A session = saved record (name, display, provider, start folder, command, autostart) + the tmux window
of the same name in the `main` tmux session of that host, running `claude` in that folder.
Statuses come from `tmux capture-pane` every DEVBOX_POLL seconds.

Data (DEVBOX_DATA/users/<user>/):
  settings.json   API keys (env), default command
  hosts.json      remote hosts
  messages.jsonl  Team board (shared by all hosts of the user)
  ssh/            console key + known_hosts
  boxes/<host>/   sessions.json, activity.json (per host)
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import shlex
import signal
import struct
import tempfile
import termios
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .secretstore import make_store

try:  # libpam is not needed (and may be missing) in the Docker image
    from . import pam
except OSError:  # pragma: no cover
    pam = None

# ---------------------------------------------------------------- config

IS_ROOT = os.geteuid() == 0
RUN_USER = pwd.getpwuid(os.geteuid()).pw_name
NODENAME = os.uname().nodename

HOST = os.environ.get("DEVBOX_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEVBOX_PORT", "7700"))
SESSION = os.environ.get("DEVBOX_TMUX_SESSION", "main")
POLL = float(os.environ.get("DEVBOX_POLL", "4"))  # seconds; drives the green "working" indicator
PAM_SERVICE = os.environ.get("DEVBOX_PAM_SERVICE", "login")
ALLOWED = {u.strip() for u in os.environ.get("DEVBOX_USERS", "").split(",") if u.strip()}
ALLOW_ROOT = os.environ.get("DEVBOX_ALLOW_ROOT") == "1"
SESSION_DAYS = float(os.environ.get("DEVBOX_SESSION_DAYS", "7"))
DATA = Path(os.environ.get("DEVBOX_DATA") or
            ("/var/lib/devbox-console" if IS_ROOT else Path.home() / ".local/share/devbox-console"))
PUBLIC_URL = os.environ.get("DEVBOX_PUBLIC_URL", f"http://{HOST}:{PORT}")
STATIC = Path(__file__).parent / "static"
STD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
AUTH = os.environ.get("DEVBOX_AUTH", "pam").strip().lower()  # pam | ssh
LOCAL_SSH = os.environ.get("DEVBOX_LOCAL_SSH", "host.docker.internal:22")
LOCAL_LABEL = os.environ.get("DEVBOX_LOCAL_LABEL", NODENAME)
CM_DIR = Path(tempfile.gettempdir()) / f"devbox-cm-{os.geteuid()}"  # short path: unix socket limit
RUNTIME = Path(os.environ.get("DEVBOX_RUNTIME") or ("/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir())) \
    / f"devbox-{os.geteuid()}"  # tmpfs: decrypted private keys only live here

for d in (DATA, DATA / "users", CM_DIR, RUNTIME):
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)

STORE = make_store(DATA)
# prints $DEVBOX_ASKPASS_PW; the secret itself is only in the env. Not on RUNTIME: Docker mounts tmpfs noexec.
ASKPASS = DATA / "askpass.sh"
ASKPASS.write_text('#!/bin/sh\nprintf "%s\\n" "$DEVBOX_ASKPASS_PW"\n')
os.chmod(ASKPASS, 0o700)


def local_host(username: str) -> dict:
    """ "This machine" as an SSH target (Docker mode)."""
    hostname, _, port = LOCAL_SSH.rpartition(":")
    return {"id": "local", "label": LOCAL_LABEL, "hostname": hostname or LOCAL_SSH,
            "port": int(port) if port.isdigit() else 22, "username": username}


def askpass_env(password: str) -> dict:
    env = {**os.environ, "SSH_ASKPASS": str(ASKPASS), "SSH_ASKPASS_REQUIRE": "force",
           "DISPLAY": os.environ.get("DISPLAY", ":0"), "DEVBOX_ASKPASS_PW": password}
    env.pop("SSH_AUTH_SOCK", None)
    return env


def _read_or_create(path: Path, n: int) -> str:
    if not path.exists():
        path.write_text(secrets.token_urlsafe(n))
        os.chmod(path, 0o600)
    return path.read_text().strip()


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


SECRET = _read_or_create(DATA / "secret", 32).encode()
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
WIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
WID_RE = re.compile(r"^@\d+$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$", re.I)

SEP = "<~dvb~>"  # tmux escapes control chars in -F output, so use a printable separator
WIN_FMT = SEP.join([
    "#{window_id}", "#{window_index}", "#{window_name}", "#{pane_current_path}",
    "#{pane_current_command}", "#{pane_title}", "#{window_activity}", "#{pane_dead}", "#{pane_start_path}",
    "#{session_name}", "#{session_group}",
])
EXT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}~[A-Za-z0-9_.\-@]{1,40}$")  # "<tmux session>~<window>"
SPINNER_RE = re.compile(r"^\s*[✻✶✳✢✽✺·*◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\S+…", re.M)
WORKING_RE = re.compile(r"esc to interrupt|ctrl\+c to interrupt|Running \d+ shell|\(\d+m? ?\d*s · ", re.I)
WAITING_RE = re.compile(
    r"Do you want to|Would you like to|❯ 1\. Yes|\(y/n\)|\[y/N\]|\[Y/n\]|Press Enter|Allow (once|always)",
    re.I,
)
SHELLS = {"bash", "zsh", "sh", "fish", "ash", "dash"}


def mask(v: str) -> str:
    return "•" * 8 + v[-4:] if len(v) > 8 else "•" * len(v)


def login_cmd(cmd: str) -> str:
    """Run the session command through a login shell so PATH (e.g. ~/.local/bin/claude) is right."""
    return f'exec "${{SHELL:-/bin/bash}}" -lc {shlex.quote(cmd)}'


def classify(text: str, cmd: str, changed: bool) -> str:
    tail = "\n".join(text.rstrip().splitlines()[-25:])
    if WAITING_RE.search(tail):
        return "waiting"
    if SPINNER_RE.search(tail) or WORKING_RE.search(tail):
        return "working"
    if changed:
        return "working"
    if cmd in SHELLS:
        return "shell"
    return "idle"


# ---------------------------------------------------------------- a box = one machine of one user


class Box:
    """tmux + sessions of one console user on one machine (local or remote over SSH)."""

    def __init__(self, user: "User", host: dict | None) -> None:
        self.user = user
        self.host = host  # None = this machine, run directly (PAM mode)
        self.id = host["id"] if host else "local"
        self.via_ssh = host is not None  # remote hosts, and "local" in Docker mode
        self.dir = user.dir / "boxes" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.windows: list[dict] = []
        self.error: str | None = None
        self.hashes: dict[str, str] = {}
        self.activity: dict[str, dict] = _load_json(self.dir / "activity.json", {})
        self.listeners: set[asyncio.Queue] = set()
        self.wake = asyncio.Event()
        self.dir_cache: dict[str, tuple[float, list[str]]] = {}
        self.poller: asyncio.Task | None = None
        self.last_ok = 0.0

    @property
    def remote(self) -> bool:
        """Another machine (UI meaning); "local" is this machine even when reached over SSH."""
        return self.id != "local"

    @property
    def label(self) -> str:
        if not self.host:
            return NODENAME
        return self.host.get("label") or self.host.get("hostname")

    # ---- command execution
    def ssh_base(self, tty: bool = False) -> list[str]:
        h = self.host
        return ["ssh", *(["-tt"] if tty else ["-T"]),
                *self.user.ssh_opts(self.user.host_key(self.id), password=bool(self.user.host_password(self.id))),
                "-p", str(h.get("port") or 22), f"{h['username']}@{h['hostname']}"]

    def ssh_env(self) -> dict:
        pw = self.user.host_password(self.id)
        if pw:
            return askpass_env(pw)
        env = {**os.environ}
        env.pop("SSH_AUTH_SOCK", None)
        return env

    def wrap(self, argv: list[str], tty: bool = False) -> tuple[list[str], dict]:
        """argv to run on this box -> (local argv, spawn kwargs)."""
        if self.via_ssh:
            return [*self.ssh_base(tty), *([shlex.join(argv)] if argv else [])], \
                {"env": self.ssh_env(), "start_new_session": True}
        return argv, self.user.spawn_kw()

    async def run(self, argv: list[str], check: bool = False, timeout: float = 25,
                  input: bytes | None = None) -> tuple[int, str]:
        cmd, kw = self.wrap(argv)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL if input is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)
        try:
            out, err = await asyncio.wait_for(proc.communicate(input), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"{self.label}: timed out")
        if self.via_ssh and proc.returncode == 255:  # ssh itself failed
            raise RuntimeError(f"{self.label}: SSH connection failed — {err.decode(errors='replace').strip()[-300:]}")
        if check and proc.returncode:
            raise RuntimeError(err.decode(errors="replace").strip() or f"{argv[0]} failed")
        return proc.returncode or 0, out.decode(errors="replace")

    async def tmux(self, *args: str, check: bool = False) -> str:
        return (await self.run(["tmux", *args], check=check))[1]

    async def sh(self, script: str, *args: str) -> tuple[int, str]:
        return await self.run(["sh", "-c", script, "sh", *args])

    # ---- storage
    def load_sessions(self) -> dict:
        return _load_json(self.dir / "sessions.json", {"sessions": {}, "recent_dirs": [], "projects_root": ""})

    def save_sessions(self, s: dict) -> None:
        _save_json(self.dir / "sessions.json", s)

    def projects_root(self, s: dict | None = None) -> str:
        s = s or self.load_sessions()
        return s.get("projects_root") or (os.environ.get("DEVBOX_PROJECTS", "~") if not self.remote else "~")

    def broadcast(self, kind: str, payload) -> None:
        for q in list(self.listeners):
            if q.qsize() < 200:
                q.put_nowait((kind, payload))

    # ---- folders
    async def resolve_dir(self, path: str) -> str | None:
        """Expand ~ and return the absolute directory, or None if it doesn't exist / isn't accessible."""
        path = path.strip()
        if not path:
            return None
        if path == "~" or path.startswith("~/"):
            rc, out = await self.sh('cd "$HOME/$1" 2>/dev/null && pwd -P', path[2:])
        else:
            rc, out = await self.sh('cd "$1" 2>/dev/null && pwd -P', path)
        return out.strip() if rc == 0 and out.strip() else None

    async def project_dirs(self, root: str) -> list[str]:
        hit = self.dir_cache.get(root)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        base = await self.resolve_dir(root)
        if not base:
            return []
        _, out = await self.sh(
            'find "$1" -mindepth 1 -maxdepth 3 -type d \\( -name ".*" -o -name node_modules -o -name __pycache__ '
            '-o -name venv -o -name .venv -o -name dist -o -name build -o -name target \\) -prune -o -type d -print '
            "2>/dev/null | head -2000", base)
        dirs = sorted(out.splitlines())
        if len(self.dir_cache) > 8:
            self.dir_cache.clear()
        self.dir_cache[root] = (time.time(), dirs)
        return dirs

    # ---- tmux state
    async def has_session(self) -> bool:
        rc, _ = await self.run(["tmux", "has-session", "-t", f"={SESSION}"])
        return rc == 0

    async def poll_once(self) -> None:
        """All tmux windows on the machine: those of `main` are our sessions, any other tmux session's windows
        are listed as "external" (started outside the console) so nothing running there goes unnoticed."""
        try:
            rc, raw = await self.run(["tmux", "list-windows", "-a", "-F", WIN_FMT])
        except Exception as e:  # noqa: BLE001  (ssh down etc.)
            self.error = str(e)
            self.windows = []
            self.broadcast("windows", self.snapshot())
            return
        self.error = None
        self.last_ok = time.time()
        if rc != 0:  # no tmux server / no session yet: that's simply "no sessions"
            raw = ""
        lines_by_wid = {}
        rows, seen = [], set()
        for line in raw.splitlines():
            r = line.split(SEP)
            if len(r) < 11 or r[0] in seen:
                continue
            sess, group = r[9], r[10]
            if sess.startswith("web-") and group:  # our own per-tab views: same windows as their group
                continue
            if group == SESSION and sess != SESSION:  # other members of main's group
                continue
            seen.add(r[0])
            rows.append(r)
        if rows:  # one round trip for all previews (matters over SSH)
            script = "; ".join(f"tmux capture-pane -p -J -t {shlex.quote(r[0])} -S -40; echo {shlex.quote(SEP + r[0])}"
                               for r in rows)
            _, caps = await self.sh(script)
            buf: list[str] = []
            for ln in caps.splitlines():
                if ln.startswith(SEP):
                    lines_by_wid[ln[len(SEP):]] = "\n".join(buf)
                    buf = []
                else:
                    buf.append(ln)
        wins, now = [], time.time()
        for wid, idx, wname, path, cmd, title, act, dead, start_path, tsess, _grp in (r[:11] for r in rows):
            external = tsess != SESSION
            if external:
                wpart = wname if re.fullmatch(r"[A-Za-z0-9_.\-@]{1,40}", wname) else f"@{wid[1:]}"
                spart = re.sub(r"[^A-Za-z0-9_.\-]", "_", tsess)[:40] or "_"
                name = f"{spart}~{wpart}"
            else:
                name = wname
            cap = lines_by_wid.get(wid, "")
            h = hashlib.sha1(cap.encode()).hexdigest()
            changed = wid in self.hashes and self.hashes[wid] != h
            self.hashes[wid] = h
            a = self.activity.setdefault(name, {})
            if changed or "last_change" not in a:
                a["last_change"] = now
            status = "dead" if dead == "1" else classify(cap, cmd, changed)
            a["status"], a["seen"] = status, now
            lines = [ln for ln in cap.rstrip().splitlines() if ln.strip()]
            wins.append({
                "id": wid, "index": int(idx or 0), "name": name, "path": path, "cmd": cmd,
                "title": "" if title in ("", name) or "@" in title.split(":")[0] or title == NODENAME else title,
                "activity": int(act or 0), "status": status, "last_change": a["last_change"],
                "preview": lines[-8:], "start_path": start_path,
                "tmux_session": tsess, "window_name": wname, "external": external,
            })
        self.windows = sorted(wins, key=lambda w: (w["external"], w["tmux_session"], w["index"]))
        # keep memory flat: forget windows that are gone (saved sessions keep their activity)
        live_ids = {w["id"] for w in wins}
        self.hashes = {k: v for k, v in self.hashes.items() if k in live_ids}
        keep = {w["name"] for w in wins} | set(self.load_sessions()["sessions"])
        self.activity = {k: v for k, v in self.activity.items() if k in keep}
        try:
            _save_json(self.dir / "activity.json", self.activity)
        except OSError:
            pass
        self.broadcast("windows", self.snapshot())

    def snapshot(self) -> dict:
        """Saved sessions merged with live tmux windows (live-but-unsaved and saved-but-stopped included)."""
        data = self.load_sessions()
        recs, hidden = data["sessions"], set(data.get("hidden", []))
        items, live = [], set()
        for w in self.windows:
            live.add(w["name"])
            rec = recs.get(w["name"]) or {}
            items.append({
                **w, "running": True, "saved": w["name"] in recs, "hidden": w["name"] in hidden,
                "display": rec.get("display") or (f"{w['window_name']} ({w['tmux_session']})" if w["external"] else w["name"]),
                "provider": rec.get("provider", ""),
                "cwd": rec.get("cwd") or w["start_path"], "command": rec.get("command", ""),
                "autostart": bool(rec.get("autostart")),
            })
        for name, rec in recs.items():
            if name in live:
                continue
            items.append({
                "id": None, "index": 10_000, "name": name, "running": False, "saved": True, "status": "stopped",
                "hidden": name in hidden, "external": False, "tmux_session": SESSION,
                "display": rec.get("display") or name, "provider": rec.get("provider", ""),
                "cwd": rec.get("cwd", ""), "path": rec.get("cwd", ""), "command": rec.get("command", ""),
                "autostart": bool(rec.get("autostart")), "cmd": "", "title": "", "preview": [],
                "last_change": self.activity.get(name, {}).get("last_change", rec.get("created", 0)),
            })
        return {"host_id": self.id, "host": self.label, "remote": self.remote, "session": SESSION,
                "user": self.user.name, "login": (self.host or {}).get("username") or self.user.name,
                "error": self.error, "windows": items}

    def summary(self) -> dict:
        run = [w for w in self.windows]
        return {"id": self.id, "label": self.label, "remote": self.remote,
                "hostname": (self.host or {}).get("hostname", NODENAME), "port": (self.host or {}).get("port", 22),
                "username": (self.host or {}).get("username", self.user.name), "error": self.error,
                "running": len(run), "working": sum(w["status"] == "working" for w in run),
                "waiting": sum(w["status"] == "waiting" for w in run),
                "saved": len(self.load_sessions()["sessions"]), "last_ok": self.last_ok,
                "sessions": [{"name": w["name"], "display": w["display"], "status": w["status"], "running": w["running"],
                              "hidden": w["hidden"], "external": w["external"]}
                             for w in self.snapshot()["windows"]],
                "info": (self.host or {}).get("info", {}), "auth": self.user.host_auth(self.id) if self.via_ssh else "local"}

    def session_env(self) -> dict:
        return {**self.user.session_env(), "DEVBOX_HOST_ID": self.id}

    def window_env_args(self) -> list[str]:
        args = []
        for k, v in self.session_env().items():
            args += ["-e", f"{k}={v}"]
        return args

    async def apply_env(self) -> None:
        """Expose API keys + board coordinates to new tmux windows (global tmux environment)."""
        if not await self.has_session():
            return
        script = "; ".join(f"tmux set-environment -g {shlex.quote(k)} {shlex.quote(v)}"
                           for k, v in self.session_env().items())
        await self.sh(script)

    async def start_window(self, name: str, cwd: str, cmd: str) -> str:
        """Open tmux window `name` in `cwd` running `cmd` (claude) through a login shell."""
        fmt = ["-P", "-F", "#{window_id}", "-n", name, "-c", cwd, *self.window_env_args()]
        if await self.has_session():
            args = ["new-window", "-d", "-t", f"={SESSION}:", *fmt]
        else:  # first session on this box: create the tmux session with it
            args = ["new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", *fmt]
        if cmd:
            args.append(login_cmd(cmd))
        wid = (await self.tmux(*args, check=True)).strip()
        if WID_RE.fullmatch(wid):
            # keep the window when claude exits: status becomes "dead" and the page offers Restart
            await self.tmux("set-option", "-w", "-t", wid, "remain-on-exit", "on")
        await self.apply_env()
        return wid

    async def start_saved(self, name: str, resume: bool) -> str:
        rec = self.load_sessions()["sessions"].get(name)
        if rec is None:
            raise web.HTTPNotFound(text="no saved session")
        if any(w["name"] == name for w in self.windows):
            raise web.HTTPConflict(text="session is already running")
        cwd = await self.resolve_dir(rec.get("cwd") or "~")
        if not cwd:
            raise web.HTTPBadRequest(text=f"folder not found: {rec.get('cwd')}")
        cmd = rec.get("command") or ""
        if resume and cmd.split()[:1] == ["claude"] and "--continue" not in cmd and "-c" not in cmd.split():
            cmd += " --continue"  # pick the last conversation in this folder back up
        return await self.start_window(name, cwd, cmd)

    async def autostart(self) -> None:
        await self.poll_once()
        if self.error:
            return
        live = {w["name"] for w in self.windows}
        for name, rec in self.load_sessions()["sessions"].items():
            if rec.get("autostart") and name not in live:
                try:
                    await self.start_saved(name, resume=True)
                except Exception as e:  # noqa: BLE001
                    self.error = f"{name} could not be autostarted: {getattr(e, 'text', e)}"

    async def poll_loop(self) -> None:
        started = False
        while True:
            try:
                if not started:
                    await self.autostart()
                    started = not self.error
                else:
                    await self.poll_once()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
            self.user.broadcast_hosts()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), POLL if not self.error else max(POLL, 30))
            except asyncio.TimeoutError:
                pass

    async def send_to_window(self, target_name: str, text: str) -> bool:
        wid = next((w["id"] for w in self.windows if w["name"] == target_name), None)
        if not wid:
            return False
        await self.tmux("send-keys", "-t", wid, "-l", "--", text.replace("\r", " ").replace("\n", " "))
        await asyncio.sleep(0.15)
        await self.tmux("send-keys", "-t", wid, "Enter")
        return True

    def stop(self) -> None:
        if self.poller:
            self.poller.cancel()


# ---------------------------------------------------------------- console user


class User:
    """A logged-in Linux user of this machine: owns settings, SSH key, hosts, board, boxes."""

    def __init__(self, name: str) -> None:
        self.name = name
        if AUTH == "pam":
            pw = pwd.getpwnam(name)
            self.uid, self.gid = pw.pw_uid, pw.pw_gid
            self.home, self.shell = pw.pw_dir, pw.pw_shell or "/bin/bash"
        else:  # Docker: the user lives on the host, reached over SSH
            self.uid = self.gid = None
            self.home, self.shell = "/", "/bin/sh"
        self.dir = DATA / "users" / name
        self.rt = RUNTIME / "users" / name
        for d in (self.dir, self.dir / "ssh", self.dir / "boxes", self.rt):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self.token = _read_or_create(self.dir / "api_token", 24)
        self.boxes: dict[str, Box] = {}
        self.host_listeners: set[asyncio.Queue] = set()
        self.msg_lock = asyncio.Lock()
        self.env: dict[str, str] = {}  # API keys etc. (secret store)
        self.host_secrets: dict[str, dict] = {}  # host id -> {private_key?, password?} (secret store)
        self._migrate()

    def sp(self, *parts: str) -> str:
        """Secret store path."""
        return "/".join(["users", self.name, *parts])

    async def load(self) -> None:
        """Pull secrets from the store; move secrets left in files by older versions into it."""
        st = _load_json(self.dir / "settings.json", {})
        self.env = await STORE.get(self.sp("env")) or {}
        if st.get("env"):
            self.env = {**st.pop("env"), **self.env}
            await STORE.put(self.sp("env"), self.env)
            _save_json(self.dir / "settings.json", st)
        old_key = self.dir / "ssh" / "id_ed25519"
        if old_key.exists() and not await STORE.get(self.sp("ssh-key")):
            await STORE.put(self.sp("ssh-key"), {"private": old_key.read_text(),
                                                 "public": old_key.with_suffix(".pub").read_text().strip()})
        for f in (old_key, old_key.with_suffix(".pub")):
            f.unlink(missing_ok=True)
        for h in self.load_hosts():
            sec = await STORE.get(self.sp("hosts", h["id"])) or {}
            old = self.dir / "ssh" / f"host-{h['id']}.key"
            if old.exists():
                sec["private_key"] = old.read_text()
                await STORE.put(self.sp("hosts", h["id"]), sec)
                old.unlink()
            if sec:
                self.host_secrets[h["id"]] = sec

    def _migrate(self) -> None:
        """Older single-host layout kept sessions in settings.json."""
        s = _load_json(self.dir / "settings.json", {})
        if "sessions" in s or "windows" in s:
            box_dir = self.dir / "boxes" / "local"
            box_dir.mkdir(parents=True, exist_ok=True)
            sessions = {**s.pop("windows", {}), **s.pop("sessions", {})}
            _save_json(box_dir / "sessions.json", {"sessions": sessions, "recent_dirs": s.pop("recent_dirs", []),
                                                   "projects_root": s.pop("projects_root", "")})
            _save_json(self.dir / "settings.json", s)
        if (self.dir / "activity.json").exists():
            (self.dir / "activity.json").replace(self.dir / "boxes" / "local" / "activity.json")

    # ---- local processes as this user
    def spawn_kw(self) -> dict:
        if AUTH != "pam":
            raise RuntimeError("no local commands in Docker mode")
        env = {
            "HOME": self.home, "USER": self.name, "LOGNAME": self.name, "SHELL": self.shell,
            "PATH": STD_PATH if IS_ROOT else os.environ.get("PATH", STD_PATH),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        kw: dict = {"env": env, "cwd": self.home if os.path.isdir(self.home) else "/"}
        if IS_ROOT and self.uid != 0:
            kw.update(user=self.uid, group=self.gid, extra_groups=os.getgrouplist(self.name, self.gid))
        return kw

    # ---- ssh identity (console key per user, kept in the secret store, materialised on tmpfs)
    @property
    def key(self) -> Path:
        return self.rt / "id_ed25519"

    async def ensure_key(self) -> str:
        rec = await STORE.get(self.sp("ssh-key"))
        if not rec:
            tmp = self.rt / "new_key"
            for f in (tmp, tmp.with_suffix(".pub")):
                f.unlink(missing_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"devbox-console:{self.name}@{LOCAL_LABEL}",
                "-f", str(tmp), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode:
                raise RuntimeError("ssh-keygen: " + err.decode(errors="replace"))
            rec = {"private": tmp.read_text(), "public": tmp.with_suffix(".pub").read_text().strip()}
            await STORE.put(self.sp("ssh-key"), rec)
            for f in (tmp, tmp.with_suffix(".pub")):
                f.unlink(missing_ok=True)
        if not self.key.exists():
            self._write_private(self.key, rec["private"])
        return rec["public"]

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    def host_key(self, hid: str) -> Path:
        """Per-host private key the user pasted (materialised on tmpfs), else the console key."""
        pk = self.host_secrets.get(hid, {}).get("private_key")
        if not pk:
            return self.key
        f = self.rt / f"host-{hid}.key"
        if not f.exists():
            self._write_private(f, pk)
        return f

    def host_password(self, hid: str) -> str | None:
        return self.host_secrets.get(hid, {}).get("password")

    def host_auth(self, hid: str) -> str:
        sec = self.host_secrets.get(hid, {})
        return "password" if sec.get("password") else "custom-key" if sec.get("private_key") else "console-key"

    async def set_host_secret(self, hid: str, data: dict | None) -> None:
        (self.rt / f"host-{hid}.key").unlink(missing_ok=True)
        if data:
            await STORE.put(self.sp("hosts", hid), data)
            self.host_secrets[hid] = data
        else:
            await STORE.delete(self.sp("hosts", hid))
            self.host_secrets.pop(hid, None)

    def ssh_opts(self, key: Path | None = None, password: bool = False) -> list[str]:
        auth = (["-o", "BatchMode=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                 "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1"] if password else
                ["-i", str(key or self.key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"])
        return ["-F", "none", *auth,
                "-o", f"UserKnownHostsFile={self.dir / 'ssh' / 'known_hosts'}", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=20",
                "-o", "ControlMaster=auto", "-o", f"ControlPath={CM_DIR}/%C", "-o", "ControlPersist=10m",
                "-o", "LogLevel=ERROR"]

    # ---- settings
    def load_settings(self) -> dict:
        s = _load_json(self.dir / "settings.json", {})
        s.setdefault("new_window_command", os.environ.get("DEVBOX_DEFAULT_CMD", "claude"))
        return s

    def save_settings(self, s: dict) -> None:
        s.pop("env", None)  # secrets never go to the plain settings file
        _save_json(self.dir / "settings.json", s)

    async def set_env(self, name: str, value: str | None) -> None:
        env = dict(self.env)
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
        await STORE.put(self.sp("env"), env)
        self.env = env

    def session_env(self) -> dict:
        return {**self.env, "DEVBOX_URL": PUBLIC_URL, "DEVBOX_TOKEN": self.token}

    def load_hosts(self) -> list[dict]:
        return _load_json(self.dir / "hosts.json", [])

    def save_hosts(self, hosts: list[dict]) -> None:
        _save_json(self.dir / "hosts.json", hosts)

    # ---- boxes
    def box(self, host_id: str = "local") -> Box:
        if host_id not in self.boxes:
            host = local_host(self.name) if AUTH == "ssh" and host_id == "local" else None
            if host_id != "local":
                host = next((h for h in self.load_hosts() if h["id"] == host_id), None)
                if host is None:
                    raise web.HTTPNotFound(text="no such host")
            self.boxes[host_id] = Box(self, host)
        b = self.boxes[host_id]
        if b.poller is None or b.poller.done():
            b.poller = asyncio.get_running_loop().create_task(b.poll_loop())
        return b

    def all_boxes(self) -> list[Box]:
        return [self.box("local"), *(self.box(h["id"]) for h in self.load_hosts())]

    def drop_box(self, host_id: str) -> None:
        b = self.boxes.pop(host_id, None)
        if b:
            b.stop()

    def hosts_summary(self) -> list[dict]:
        return [b.summary() for b in self.all_boxes()]

    def broadcast_hosts(self) -> None:
        data = self.hosts_summary()
        for q in list(self.host_listeners):
            if q.qsize() < 200:
                q.put_nowait(("hosts", data))

    # ---- board
    def load_messages(self) -> list[dict]:
        out = []
        try:
            with (self.dir / "messages.jsonl").open() as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out


USERS: dict[str, User] = {}


_user_lock = asyncio.Lock()


async def get_user(name: str) -> User:
    u = USERS.get(name)
    if u is None:
        async with _user_lock:
            u = USERS.get(name)
            if u is None:
                u = User(name)
                await u.load()
                USERS[name] = u
                u.all_boxes()  # start pollers for every host
    return u


def user_by_token(token: str) -> str | None:
    for d in (DATA / "users").iterdir():
        f = d / "api_token"
        if f.exists() and hmac.compare_digest(f.read_text().strip(), token):
            return d.name
    return None


# ---------------------------------------------------------------- auth


def may_login(name: str) -> str | None:
    """Return an error message if this account may not use the console."""
    if not USER_RE.fullmatch(name):
        return "invalid user name"
    if AUTH == "ssh":  # users live on the host; its sshd decides
        if name == "root" and not ALLOW_ROOT:
            return "root login is disabled (DEVBOX_ALLOW_ROOT=1)"
        if ALLOWED and name not in ALLOWED:
            return "this user is not allowed (DEVBOX_USERS)"
        return None
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None  # let PAM fail it with the same timing as a wrong password
    if pw.pw_uid == 0 and not ALLOW_ROOT:
        return "root login is disabled (DEVBOX_ALLOW_ROOT=1)"
    if ALLOWED and name not in ALLOWED:
        return "this user is not allowed (DEVBOX_USERS)"
    if not IS_ROOT and name != RUN_USER:
        return f"the service is not running as root; only '{RUN_USER}' can log in"
    return None


def sign(value: str) -> str:
    return value + "." + hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


def cookie_user(c: str | None) -> str | None:
    if not c or "." not in c:
        return None
    value, _ = c.rsplit(".", 1)
    if not hmac.compare_digest(sign(value), c):
        return None
    try:
        name, exp = value.split("|")
        if float(exp) > time.time():
            return name
    except ValueError:
        pass
    return None


PUBLIC_PATHS = {"/login", "/api/login", "/api/whoami", "/devbox-msg"}


@web.middleware
async def auth_mw(req: web.Request, handler):
    p = req.path
    if p in PUBLIC_PATHS or p.startswith("/static/"):
        return await handler(req)
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        name = user_by_token(auth[7:].strip())
    else:
        name = cookie_user(req.cookies.get("devbox_auth"))
        origin = req.headers.get("Origin")  # cross-site protection (cookie is SameSite=Strict too)
        if name and req.method not in ("GET", "HEAD") and origin and origin.split("://", 1)[-1] != req.host:
            raise web.HTTPForbidden(text="bad origin")
    if not name:
        if p.startswith("/api/") or p.startswith("/ws/"):
            raise web.HTTPUnauthorized(text="login required")
        raise web.HTTPFound("/login")
    req["user"] = await get_user(name)
    return await handler(req)


def U(req) -> User:
    return req["user"]


def B(req) -> Box:
    host = req.query.get("host") or "local"
    if host != "local" and not HOST_ID_RE.fullmatch(host):
        raise web.HTTPBadRequest(text="bad host")
    return U(req).box(host)


def wid_of(req) -> str:
    wid = req.match_info["wid"]
    if not WID_RE.fullmatch(wid):
        raise web.HTTPBadRequest(text="bad window id")
    return wid


def name_of(req) -> str:
    name = req.match_info["name"]
    if not (WIN_NAME_RE.fullmatch(name) or EXT_NAME_RE.fullmatch(name)):
        raise web.HTTPBadRequest(text="invalid session name")
    return name


# ---------------------------------------------------------------- pages / login


async def index(req):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def devbox_msg(req):
    """The Team board CLI, so sessions on any machine can `curl` it (not a secret)."""
    return web.FileResponse(Path(__file__).parent.parent / "devbox-msg",
                            headers={"Content-Type": "text/x-python", "Cache-Control": "no-cache"})


async def login_page(req):
    return web.FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


_fail: dict[str, list[float]] = {}


async def api_login(req):
    body = await req.json()
    name = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    ip = req.remote or "?"
    for k in [k for k, v in _fail.items() if not v or time.time() - v[-1] > 300]:
        del _fail[k]
    recent = [t for t in _fail.get(ip, []) if time.time() - t < 300]
    if len(recent) >= 10:
        raise web.HTTPTooManyRequests(text="too many failed attempts, wait 5 minutes")
    err = may_login(name)
    if err:
        raise web.HTTPForbidden(text=err)
    if AUTH == "ssh":
        # the host's sshd (PAM) checks the password; then the console key is installed for this user
        rc, msg = await _ssh_password(local_host(name), password, "true", known_hosts=DATA / "known_hosts")
        ok = rc == 0
        if rc == 255 and "Permission denied" not in msg:
            raise web.HTTPBadGateway(text=f"cannot SSH to the host ({LOCAL_SSH}): {msg[-200:]}")
    else:
        ok, _msg = await asyncio.to_thread(pam.authenticate, name, password, PAM_SERVICE)
    if not ok:
        _fail[ip] = recent + [time.time()]
        await asyncio.sleep(1)
        raise web.HTTPUnauthorized(text="login failed, please try again")
    _fail.pop(ip, None)
    u = await get_user(name)
    if AUTH == "ssh":
        await _install_key(u, local_host(name), password)
        u.drop_box("local")
        u.box("local")
    resp = web.json_response({"ok": True, "user": name})
    resp.set_cookie("devbox_auth", sign(f"{name}|{time.time() + SESSION_DAYS * 86400}"),
                    max_age=int(SESSION_DAYS * 86400), httponly=True, samesite="Strict",
                    secure=PUBLIC_URL.startswith("https"))
    return resp


async def api_whoami(req):
    return web.json_response({"user": cookie_user(req.cookies.get("devbox_auth")), "host": LOCAL_LABEL,
                              "auth": AUTH, "single_user": None if IS_ROOT or AUTH == "ssh" else RUN_USER})


async def api_logout(req):
    resp = web.json_response({"ok": True})
    resp.del_cookie("devbox_auth")
    return resp


# ---------------------------------------------------------------- hosts (remote machines)


def _host_fields(body: dict) -> dict:
    hostname = str(body.get("hostname", "")).strip()
    username = str(body.get("username", "")).strip()
    try:
        port = int(body.get("port") or 22)
    except ValueError:
        port = 0
    if not HOSTNAME_RE.fullmatch(hostname) or hostname.startswith("-"):
        raise web.HTTPBadRequest(text="invalid host name / IP")
    if not USER_RE.fullmatch(username):
        raise web.HTTPBadRequest(text="invalid user name")
    if not 0 < port < 65536:
        raise web.HTTPBadRequest(text="invalid port")
    return {"label": str(body.get("label", "")).strip()[:40] or hostname, "hostname": hostname,
            "port": port, "username": username}


async def _ssh_password(h: dict, password: str, remote_cmd: str, stdin: bytes = b"",
                        known_hosts: Path | None = None) -> tuple[int, str]:
    """One password-authenticated ssh call (SSH_ASKPASS, no tty). Returns (rc, stderr)."""
    opts = ["-F", "none", "-o", "BatchMode=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1", "-o", "ControlMaster=no",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR"]
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-T", *opts, "-p", str(h["port"]), f"{h['username']}@{h['hostname']}", remote_cmd,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=askpass_env(password), start_new_session=True)  # no controlling tty -> ssh must use SSH_ASKPASS
    try:
        _, err = await asyncio.wait_for(proc.communicate(stdin), 30)
    except asyncio.TimeoutError:
        proc.kill()
        return 255, "connection timed out"
    return proc.returncode or 0, err.decode(errors="replace").strip()


async def _install_key(u: User, h: dict, password: str) -> None:
    """Use the password ONCE to append the user's console key to the remote authorized_keys."""
    pub = await u.ensure_key()
    remote = ('umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && '
              'k=$(cat) && (grep -qxF "$k" ~/.ssh/authorized_keys || printf "%s\\n" "$k" >> ~/.ssh/authorized_keys)')
    rc, msg = await _ssh_password(h, password, remote, (pub + "\n").encode(), u.dir / "ssh" / "known_hosts")
    if rc:
        if "Permission denied" in msg:
            msg = "password rejected (or password login is disabled on the server)"
        raise web.HTTPBadGateway(text=f"could not install the key: {msg[-300:]}")


async def _apply_host_auth(u: User, hid: str, h: dict, body: dict) -> None:
    """Credentials from the host form:
       password (+ store_password=false) -> install the console key, password is NOT kept
       password + store_password=true    -> keep the password in the secret store (Vault), use it each time
       private_key                        -> keep the pasted key in the secret store
       nothing                            -> keep what is there (or the manually installed console key)"""
    pk = str(body.get("private_key") or "").strip()
    pw = str(body.get("password") or "")
    if pk:
        if "PRIVATE KEY" not in pk:
            raise web.HTTPBadRequest(text="the private key must be in OpenSSH/PEM format")
        if "ENCRYPTED" in pk:
            raise web.HTTPBadRequest(text="passphrase-protected keys are not supported; use password-based key install")
        await u.set_host_secret(hid, {"private_key": pk + "\n"})
    elif pw and body.get("store_password"):
        await u.set_host_secret(hid, {"password": pw})
    elif pw:
        await _install_key(u, h, pw)
        await u.set_host_secret(hid, None)  # back to the console key
    elif body.get("auth") == "manual":
        await u.set_host_secret(hid, None)


async def _probe(box: Box) -> dict:
    """Check the connection and what's installed on the host."""
    rc, out = await box.sh(
        'echo "os=$(uname -sm)"; echo "host=$(hostname)"; '
        'echo "tmux=$(tmux -V 2>/dev/null)"; '
        'echo "claude=$("${SHELL:-/bin/sh}" -lc "claude --version" 2>/dev/null | head -1)"')
    info = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    return {"ok": rc == 0, **info}


async def api_hosts(req):
    return web.json_response({"hosts": U(req).hosts_summary()})


async def api_ssh_key(req):
    return web.json_response({"public_key": await U(req).ensure_key()})


async def api_create_host(req):
    u, body = U(req), await req.json()
    h = _host_fields(body)
    hosts = u.load_hosts()
    dup = next((x for x in hosts if (x["hostname"], x["port"], x["username"]) == (h["hostname"], h["port"], h["username"])), None)
    if dup:
        raise web.HTTPConflict(text=f"this host is already added: {dup['label']}")
    base = re.sub(r"[^a-z0-9-]+", "-", h["label"].lower()).strip("-")[:24] or "host"
    hid, n = base, 2
    while hid == "local" or any(x["id"] == hid for x in hosts):
        hid, n = f"{base}-{n}", n + 1
    h = {"id": hid, **h, "created": time.time()}
    await u.ensure_key()
    await _apply_host_auth(u, hid, h, body)
    box = Box(u, h)
    try:
        info = await _probe(box)
    except RuntimeError as e:
        await u.set_host_secret(hid, None)
        raise web.HTTPBadGateway(text=f"{e} — enter a password to install the key, paste a private key, or add the public key manually")
    h["info"] = info
    hosts.append(h)
    u.save_hosts(hosts)
    u.box(hid)
    u.broadcast_hosts()
    return web.json_response({"host": u.box(hid).summary(), "hosts": u.hosts_summary()})


async def api_update_host(req):
    u, hid, body = U(req), req.match_info["hid"], await req.json()
    hosts = u.load_hosts()
    h = next((x for x in hosts if x["id"] == hid), None)
    if not h:
        raise web.HTTPNotFound(text="no such host")
    h.update(_host_fields({**h, **body}))
    await _apply_host_auth(u, hid, h, body)
    u.save_hosts(hosts)
    u.drop_box(hid)
    try:
        h["info"] = await _probe(u.box(hid))
        u.save_hosts(hosts)
    except RuntimeError as e:
        raise web.HTTPBadGateway(text=str(e))
    u.broadcast_hosts()
    return web.json_response({"hosts": u.hosts_summary()})


async def api_test_host(req):
    u, hid = U(req), req.match_info["hid"]
    try:
        info = await _probe(u.box(hid))
    except RuntimeError as e:
        raise web.HTTPBadGateway(text=str(e))
    hosts = u.load_hosts()
    for h in hosts:
        if h["id"] == hid:
            h["info"] = info
    u.save_hosts(hosts)
    u.box(hid).wake.set()
    return web.json_response(info)


async def api_delete_host(req):
    """Forget a host (its tmux sessions keep running there; nothing on the remote is touched)."""
    u, hid = U(req), req.match_info["hid"]
    if hid == "local":
        raise web.HTTPBadRequest(text="this host cannot be deleted")
    u.save_hosts([h for h in u.load_hosts() if h["id"] != hid])
    await u.set_host_secret(hid, None)
    u.drop_box(hid)
    u.broadcast_hosts()
    return web.json_response({"hosts": u.hosts_summary()})


# ---------------------------------------------------------------- sessions CRUD (per host)


async def api_windows(req):
    b = B(req)
    if req.query.get("refresh"):
        await b.poll_once()
    return web.json_response(b.snapshot())


async def api_capture(req):
    lines = max(10, min(int(req.query.get("lines", "300")), 5000))
    text = await B(req).tmux("capture-pane", "-p", "-J", "-t", wid_of(req), "-S", f"-{lines}")
    return web.json_response({"text": text.rstrip() + "\n"})


async def api_create_session(req):
    """Create (save) a session and, unless start=false, open it: tmux window + claude in its folder."""
    b, body = B(req), await req.json()
    name = str(body.get("name", "")).strip()
    if not WIN_NAME_RE.fullmatch(name):
        raise web.HTTPBadRequest(text="name: letters, digits, . _ - (max 40)")
    s = b.load_sessions()
    if name in s["sessions"] or any(w["name"] == name for w in b.windows):
        raise web.HTTPConflict(text="a session with this name already exists")
    cwd = await b.resolve_dir(str(body.get("cwd") or ""))
    if not cwd:
        raise web.HTTPBadRequest(text="start folder not found (claude opens in this folder)")
    cmd = body.get("command")
    s["sessions"][name] = {
        "display": str(body.get("display", "")).strip()[:60], "provider": str(body.get("provider", "")).strip()[:30],
        "command": str(cmd if cmd is not None else U(req).load_settings()["new_window_command"]).strip()[:300],
        "autostart": bool(body.get("autostart", False)), "cwd": cwd, "created": time.time(),
    }
    s["recent_dirs"] = [cwd] + [d for d in s.get("recent_dirs", []) if d != cwd][:9]
    b.save_sessions(s)
    wid = await b.start_saved(name, resume=False) if body.get("start", True) else None
    await b.poll_once()
    return web.json_response({**b.snapshot(), "created": wid})


async def api_update_session(req):
    """Edit a session; also adopts a live unsaved window (saves it). Renaming renames the tmux window."""
    b, name, body = B(req), name_of(req), await req.json()
    if "~" in name:
        raise web.HTTPBadRequest(text="external tmux windows cannot be edited (you can hide or close them)")
    s = b.load_sessions()
    live = next((w for w in b.windows if w["name"] == name), None)
    rec = s["sessions"].get(name)
    if rec is None and live is None:
        raise web.HTTPNotFound(text="no such session")
    rec = dict(rec or {"cwd": live["start_path"], "created": time.time(), "command": ""})
    for k in ("display", "provider", "command"):
        if k in body:
            rec[k] = str(body[k]).strip()[: 300 if k == "command" else 60]
    if "autostart" in body:
        rec["autostart"] = bool(body["autostart"])
    if body.get("cwd"):
        cwd = await b.resolve_dir(str(body["cwd"]))
        if not cwd:
            raise web.HTTPBadRequest(text="start folder not found")
        rec["cwd"] = cwd
    new = str(body.get("new_name") or name).strip()
    if new != name:
        if not WIN_NAME_RE.fullmatch(new):
            raise web.HTTPBadRequest(text="name: letters, digits, . _ - (max 40)")
        if new in s["sessions"] or any(w["name"] == new for w in b.windows):
            raise web.HTTPConflict(text="a session with this name already exists")
        if live:
            await b.tmux("rename-window", "-t", live["id"], new, check=True)
        s["sessions"].pop(name, None)
        if name in b.activity:
            b.activity[new] = b.activity.pop(name)
    s["sessions"][new] = rec
    b.save_sessions(s)
    await b.poll_once()
    return web.json_response(b.snapshot())


async def api_delete_session(req):
    """Delete: close the tmux window (ends claude) and forget the saved record."""
    b, name = B(req), name_of(req)
    live = next((w for w in b.windows if w["name"] == name), None)
    if live:
        await b.tmux("kill-window", "-t", live["id"], check=True)
    s = b.load_sessions()
    s["sessions"].pop(name, None)
    s["hidden"] = [n for n in s.get("hidden", []) if n != name]
    b.save_sessions(s)
    b.activity.pop(name, None)
    await b.poll_once()
    return web.json_response(b.snapshot())


async def api_start_session(req):
    b, name, body = B(req), name_of(req), await req.json()
    wid = await b.start_saved(name, resume=bool(body.get("resume", True)))
    await b.poll_once()
    return web.json_response({**b.snapshot(), "created": wid})


async def api_stop_session(req):
    """Stop: close the tmux window but keep the saved record (can be started again later)."""
    b, name = B(req), name_of(req)
    live = next((w for w in b.windows if w["name"] == name), None)
    if not live:
        raise web.HTTPConflict(text="session is already stopped")
    s = b.load_sessions()
    if name not in s["sessions"] and not live["external"]:  # stopping an unsaved window would lose it: save it first
        s["sessions"][name] = {"display": "", "provider": "", "cwd": live["start_path"], "command": "",
                               "autostart": False, "created": time.time()}
        b.save_sessions(s)
    await b.tmux("kill-window", "-t", live["id"], check=True)
    await b.poll_once()
    return web.json_response(b.snapshot())


async def api_hide_session(req):
    """Only remove from / bring back to the view: the tmux window keeps running on the machine."""
    b, name, body = B(req), name_of(req), await req.json()
    s = b.load_sessions()
    hidden = [n for n in s.get("hidden", []) if n != name]
    if body.get("hidden", True):
        hidden.append(name)
    s["hidden"] = hidden
    b.save_sessions(s)
    b.broadcast("windows", b.snapshot())
    b.user.broadcast_hosts()
    return web.json_response(b.snapshot())


async def api_window_action(req):
    """Inside a running session: interrupt, respawn (restart claude), send a prompt/command."""
    b, wid, body = B(req), wid_of(req), await req.json()
    action = body.get("action")
    if action == "interrupt":
        await b.tmux("send-keys", "-t", wid, "C-c", check=True)
    elif action == "respawn":
        args = ["respawn-pane", "-k", "-t", wid]
        start = (await b.tmux("display-message", "-p", "-t", wid, "#{pane_start_path}")).strip()
        if start:  # restart in the folder the session was started in
            args += ["-c", start]
        args += b.window_env_args()
        cmd = str(body.get("command") or "").strip()
        if cmd:
            args.append(login_cmd(cmd))
        await b.tmux(*args, check=True)
    elif action == "send":
        await b.tmux("send-keys", "-t", wid, "-l", "--", str(body.get("text", "")), check=True)
        if body.get("enter"):
            await asyncio.sleep(0.15)
            await b.tmux("send-keys", "-t", wid, "Enter", check=True)
    else:
        raise web.HTTPBadRequest(text="unknown action")
    b.wake.set()
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- uploads (images sent from the browser)

UPLOAD_MAX = 10 * 1024 * 1024
UPLOAD_KEEP_MIN = 7 * 24 * 60  # uploads older than 7 days are removed on the next upload
UPLOAD_MAGIC = [(b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif")]
# $1 = tmux window id or "" (plain shell), $2 = file name. The image arrives on stdin.
# Goes to <folder the pane is in>/.devbox/uploads, kept out of git via .git/info/exclude.
UPLOAD_SH = r"""
dir=
if [ -n "$1" ]; then
  dir=$(tmux display-message -p -t "$1" '#{pane_current_path}' 2>/dev/null)
  [ -d "$dir" ] || dir=$(tmux display-message -p -t "$1" '#{pane_start_path}' 2>/dev/null)
fi
[ -d "$dir" ] || dir=$HOME
up="$dir/.devbox/uploads"
mkdir -p "$up" || exit 3
find "$up" -maxdepth 1 -type f -mmin +$3 -delete 2>/dev/null
ex=$(cd "$dir" && git rev-parse --git-path info/exclude 2>/dev/null)
case $ex in ""|/*) ;; *) ex="$dir/$ex" ;; esac
if [ -n "$ex" ] && ! grep -qxF '.devbox/' "$ex" 2>/dev/null; then
  mkdir -p "$(dirname "$ex")" && printf '.devbox/\n' >> "$ex"
fi
cat > "$up/$2" || exit 4
printf '%s' "$up/$2"
"""


def image_ext(head: bytes) -> str | None:
    for magic, ext in UPLOAD_MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


async def api_upload(req):
    """Save an image into the folder the session's pane is in; returns the path to put into the prompt."""
    b = B(req)
    wid = req.query.get("window", "")
    if wid and not WID_RE.fullmatch(wid):
        raise web.HTTPBadRequest(text="bad window id")
    data = bytearray()
    async for chunk in req.content.iter_chunked(64 * 1024):
        data += chunk
        if len(data) > UPLOAD_MAX:
            raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_MAX, actual_size=len(data))
    ext = image_ext(bytes(data[:16]))
    if not ext:
        raise web.HTTPBadRequest(text="only PNG, JPEG, GIF or WebP images")
    name = time.strftime("img-%Y%m%d-%H%M%S-") + secrets.token_hex(2) + "." + ext
    rc, out = await b.run(["sh", "-c", UPLOAD_SH, "sh", wid, name, str(UPLOAD_KEEP_MIN)], timeout=90, input=bytes(data))
    path = out.strip()
    if rc or not path:
        raise web.HTTPInternalServerError(text=f"could not save the image on {b.label} (exit {rc})")
    return web.json_response({"path": path, "name": name, "size": len(data)})


async def api_dirs(req):
    b = B(req)
    s = b.load_sessions()
    root = b.projects_root(s)
    return web.json_response({"root": root, "recent": s.get("recent_dirs", []), "dirs": await b.project_dirs(root)})


# ---------------------------------------------------------------- settings / env


async def api_get_settings(req):
    u, b = U(req), B(req)
    s = u.load_settings()
    return web.json_response({
        "env": [{"name": k, "masked": mask(v)} for k, v in sorted(u.env.items())],
        "secrets": STORE.status(), "auth": AUTH,
        "new_window_command": s["new_window_command"], "projects_root": b.projects_root(),
        "token": u.token, "public_url": PUBLIC_URL, "session": SESSION, "host": b.label, "host_id": b.id,
        "user": u.name, "public_key": await u.ensure_key(),
    })


async def api_put_settings(req):
    u, b, body = U(req), B(req), await req.json()
    if "projects_root" in body:
        s = b.load_sessions()
        s["projects_root"] = str(body["projects_root"]).strip()[:300]
        b.save_sessions(s)
        b.dir_cache.clear()
    if "new_window_command" in body:
        st = u.load_settings()
        st["new_window_command"] = str(body["new_window_command"])[:300]
        u.save_settings(st)
    return web.json_response({"ok": True})


async def api_put_env(req):
    u, body = U(req), await req.json()
    name = str(body.get("name", "")).strip()
    if not ENV_NAME_RE.fullmatch(name) or name in ("DEVBOX_URL", "DEVBOX_TOKEN"):
        raise web.HTTPBadRequest(text="invalid variable name")
    if body.get("delete"):
        await u.set_env(name, None)
    else:
        value = str(body.get("value", ""))
        if not value:
            raise web.HTTPBadRequest(text="empty value")
        await u.set_env(name, value)
    for b in list(u.boxes.values()):  # push to every reachable host
        try:
            if body.get("delete"):
                await b.run(["tmux", "set-environment", "-g", "-u", name])
            else:
                await b.apply_env()
        except Exception:  # noqa: BLE001
            pass
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- team board (per user, all hosts)


def _split_target(t: str) -> tuple[str, str]:
    """'name' (local) or 'name@host-id'."""
    name, _, host = t.partition("@")
    return name, host or "local"


async def api_messages(req):
    u = U(req)
    msgs = u.load_messages()
    since = int(req.query.get("since", "0") or 0)
    msgs = [m for m in msgs if m["id"] > since]
    to = req.query.get("to")
    if to:
        msgs = [m for m in msgs if m.get("to") in ("", "all", to) or m.get("from") == to]
    limit = min(int(req.query.get("limit", "200")), 1000)
    return web.json_response({"messages": msgs[-limit:]})


async def api_post_message(req):
    u, body = U(req), await req.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise web.HTTPBadRequest(text="empty message")
    async with u.msg_lock:
        msgs = u.load_messages()
        m = {
            "id": (msgs[-1]["id"] + 1) if msgs else 1, "ts": time.time(),
            "from": str(body.get("from") or "user")[:80], "to": str(body.get("to") or "all")[:80],
            "text": text[:8000], "delivered": [],
        }
        if body.get("deliver"):
            if m["to"] == "all":
                targets = [(w["name"], b) for b in u.boxes.values() for w in b.windows]
            else:
                tname, thost = _split_target(m["to"])
                targets = [(tname, u.boxes[thost])] if thost in u.boxes else []
            for tname, b in targets:
                label = tname if b.id == "local" else f"{tname}@{b.id}"
                if label != m["from"] and await b.send_to_window(tname, f"[team] {m['from']} → {m['to']}: {text}"):
                    m["delivered"].append(label)
        with (u.dir / "messages.jsonl").open("a") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    for b in u.boxes.values():
        b.broadcast("message", m)
    return web.json_response(m)


async def api_events(req):
    u, b = U(req), B(req)
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(req)
    q: asyncio.Queue = asyncio.Queue()
    b.listeners.add(q)
    u.host_listeners.add(q)
    try:
        await resp.write(f"event: windows\ndata: {json.dumps(b.snapshot())}\n\n".encode())
        await resp.write(f"event: hosts\ndata: {json.dumps(u.hosts_summary())}\n\n".encode())
        while True:
            try:
                kind, payload = await asyncio.wait_for(q.get(), 25)
                await resp.write(f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        b.listeners.discard(q)
        u.host_listeners.discard(q)
    return resp


# ---------------------------------------------------------------- terminal websocket


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _make_ctty() -> None:  # runs in the child after setsid (and after dropping privileges)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def ws_term(req):
    u, b = U(req), B(req)
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(req)
    wid = req.query.get("window", "")
    cols = max(20, min(int(req.query.get("cols", "120")), 500))
    rows = max(5, min(int(req.query.get("rows", "40")), 300))
    shell_mode = req.query.get("shell") == "1"
    view = "web-" + secrets.token_hex(3)
    try:
        if shell_mode:  # plain login shell, outside tmux
            if b.via_ssh:
                argv, kw = b.wrap([], tty=True)
            else:
                argv, kw = [u.shell, "-l"], u.spawn_kw()
        else:
            tsess = next((w["tmux_session"] for w in b.windows if w["id"] == wid), SESSION)
            rc, _ = await b.run(["tmux", "has-session", "-t", f"={tsess}"])
            if rc != 0:
                await ws.send_bytes("\r\n  This tmux session no longer exists.\r\n".encode())
                await ws.close()
                return ws
            # A grouped session shares the windows of `main` but has its own current window, so every
            # browser tab can look at a different window. destroy-unattached removes it on close.
            targs = ["tmux", "new-session", "-t", f"={tsess}", "-s", view,
                     ";", "set-option", "-t", view, "destroy-unattached", "on",
                     ";", "set-option", "-t", view, "status", "off"]
            if WID_RE.fullmatch(wid):
                targs += [";", "select-window", "-t", f"{view}:{wid}"]
            if b.via_ssh:
                # remote shells see TERM from ssh; force a sane one for tmux
                argv, kw = b.wrap(["env", "TERM=xterm-256color", *targs], tty=True)
            else:
                argv, kw = targs, u.spawn_kw()
    except Exception as e:  # noqa: BLE001
        await ws.send_bytes(f"\r\n  {e}\r\n".encode())
        await ws.close()
        return ws

    master, slave = os.openpty()
    set_winsize(master, rows, cols)
    kw["env"] = {**kw["env"], "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    kw.pop("start_new_session", None)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True, preexec_fn=_make_ctty, **kw)
    os.close(slave)
    os.set_blocking(master, False)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()

    def on_read():
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        out_q.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, on_read)

    async def pump():
        while True:
            data = await out_q.get()
            if not data:
                break
            while not out_q.empty() and len(data) < 256 * 1024:  # coalesce bursts
                nxt = out_q.get_nowait()
                if not nxt:
                    out_q.put_nowait(nxt)
                    break
                data += nxt
            await ws.send_bytes(data)
        await ws.close()

    pump_task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                t = m.get("t")
                if t == "in":
                    os.write(master, str(m.get("d", "")).encode())
                elif t == "bin":
                    os.write(master, base64.b64decode(m.get("d", "")))
                elif t == "resize":
                    set_winsize(master, max(5, int(m["r"])), max(20, int(m["c"])))
                elif t == "select" and not shell_mode and WID_RE.fullmatch(str(m.get("w", ""))):
                    await b.tmux("select-window", "-t", f"{view}:{m['w']}")
                elif t == "scroll" and not shell_mode:
                    # touch scrolling: drive tmux copy-mode (-e leaves it again at the bottom)
                    page, n = int(m.get("page", 0) or 0), int(m.get("n", 0) or 0)
                    up = page > 0 or n > 0
                    cmd = ("page-up" if up else "page-down") if page else ("scroll-up" if up else "scroll-down")
                    count = abs(page) or min(abs(n), 200)
                    script = (f"tmux copy-mode -e -t {view}: ; " if up else "") + \
                             f"tmux send-keys -t {view}: -X -N {count} {cmd}"
                    if count:
                        await b.sh(script)
            elif msg.type == WSMsgType.BINARY:
                os.write(master, msg.data)
    finally:
        pump_task.cancel()
        loop.remove_reader(master)
        try:
            os.close(master)
        except OSError:
            pass
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
        if not shell_mode:
            try:
                await b.run(["tmux", "kill-session", "-t", f"={view}"])
            except Exception:  # noqa: BLE001
                pass
    return ws


# ---------------------------------------------------------------- app


async def on_startup(app):
    await STORE.start()
    print(f"secrets: {STORE.status()}", flush=True)
    # bring known users up right away: status polling + autostart sessions don't wait for a login
    for d in sorted((DATA / "users").iterdir()):
        if d.is_dir() and USER_RE.fullmatch(d.name) and (d / "api_token").exists():
            try:
                await get_user(d.name)
            except Exception as e:  # noqa: BLE001  (e.g. user removed from the host)
                print(f"user {d.name}: {e}", flush=True)


async def on_cleanup(app):
    for u in USERS.values():
        for b in u.boxes.values():
            b.stop()
    await STORE.close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=2 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/login", login_page)
    r.add_get("/devbox-msg", devbox_msg)
    r.add_post("/api/login", api_login)
    r.add_get("/api/whoami", api_whoami)
    r.add_post("/api/logout", api_logout)
    r.add_get("/api/hosts", api_hosts)
    r.add_post("/api/hosts", api_create_host)
    r.add_patch("/api/hosts/{hid}", api_update_host)
    r.add_delete("/api/hosts/{hid}", api_delete_host)
    r.add_post("/api/hosts/{hid}/test", api_test_host)
    r.add_get("/api/ssh-key", api_ssh_key)
    r.add_get("/api/windows", api_windows)
    r.add_get("/api/sessions", api_windows)
    r.add_post("/api/sessions", api_create_session)
    r.add_patch("/api/sessions/{name}", api_update_session)
    r.add_delete("/api/sessions/{name}", api_delete_session)
    r.add_post("/api/sessions/{name}/start", api_start_session)
    r.add_post("/api/sessions/{name}/stop", api_stop_session)
    r.add_post("/api/sessions/{name}/hide", api_hide_session)
    r.add_get("/api/windows/{wid}/capture", api_capture)
    r.add_post("/api/windows/{wid}/action", api_window_action)
    r.add_get("/api/dirs", api_dirs)
    r.add_post("/api/upload", api_upload)
    r.add_get("/api/settings", api_get_settings)
    r.add_put("/api/settings", api_put_settings)
    r.add_put("/api/env", api_put_env)
    r.add_get("/api/messages", api_messages)
    r.add_post("/api/messages", api_post_message)
    r.add_get("/api/events", api_events)
    r.add_get("/ws/term", ws_term)
    r.add_static("/static/", STATIC)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    if AUTH == "ssh":
        mode = f"Docker/SSH auth via {LOCAL_SSH}"
    else:
        mode = "multi-user (root, PAM)" if IS_ROOT else f"single-user (PAM, only '{RUN_USER}')"
    print(f"devbox console on http://{HOST}:{PORT} — {mode}, tmux session '{SESSION}', data {DATA}")
    web.run_app(make_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
