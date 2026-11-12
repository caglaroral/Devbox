# Operations

## Deploying

The console runs with Docker Compose from a git checkout (here: `/opt/workspace/devbox` on the server).

```bash
git push origin main                       # from the development machine
# on the server
cd /opt/workspace/devbox
git fetch --tags origin && git merge --ff-only origin/main
cd src && DEVBOX_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build console
```

- `DEVBOX_COMMIT` is baked into the image (the build context has no `.git`) and shown next to the version.
- `src/.env` is the server's local configuration — never overwrite it.
- A deploy recreates the console container: open terminals drop for a few seconds (the browser says the console
  is restarting and reconnects); sessions keep running in tmux.
- The health check turns `healthy` about 30 s after start.

## Versions

`__version__` in `src/devbox_console/__init__.py`, tagged `vX.Y.Z` in git. Each release: tests pass → bump →
commit → tag → push → deploy. `/api/whoami` returns version and commit; the console prints them at startup and
logs `console.start version=… commit=…`.

## Configuration (environment)

| Variable | |
|---|---|
| `DEVBOX_AUTH` | `ssh` (Docker: logins checked by the host's sshd) or `pam` |
| `DEVBOX_LOCAL_SSH`, `DEVBOX_LOCAL_LABEL` | how "local" is reached and named |
| `DEVBOX_PUBLIC_URL` | URL sessions use to reach the console (team board) |
| `DEVBOX_USERS`, `DEVBOX_ALLOW_ROOT` | who may log in |
| `DEVBOX_PROJECTS` | default projects root |
| `DEVBOX_DATA`, `DEVBOX_RUNTIME` | data dir (volume) and runtime tmpfs |
| `DEVBOX_POLL` | tmux poll interval (s) |
| `DEVBOX_LOG_DAYS` | event log retention (default 7) |
| `DEVBOX_COMMIT` | build commit (set at build time) |
| `VAULT_ADDR` / `VAULT_TOKEN` | secret store |

The compose file mounts `/etc/localtime` so log times use the host's timezone.

## Logs

- Event log per user: `<data>/users/<name>/logs/YYYYMMDD.log` (Settings → Event log). It survives rebuilds;
  `docker logs` does not.
- `docker logs devbox-console-1` has the same lines prefixed with the user, plus startup messages.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests --ignore=tests/test_browser.py
```

- Each test starts a real console on a free port with its own data dir, an isolated tmux server (a tmux wrapper
  with its own socket), a fake `ssh` (`tests/fakebin/ssh`, host `fakehost`), a fake `claude`
  (`tests/fakebin/claude`: session ids, transcripts, SessionStart hook, `/clear`) and a fake full-screen program
  (`tests/fakebin/fullscreen`: alternate screen + mouse tracking, logs its input).
- `tests/test_browser.py` needs Node and Playwright (`tests/browser`).
- Areas: API, sessions, shells, conversations, files, uploads, event log, remote hosts, terminal WebSocket
  (classic and buffer: replay, complete fast output, exact input, sizes across devices, copy-mode, wheel
  events, restart close code).
