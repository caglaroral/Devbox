# API

All endpoints need a login cookie (`devbox_auth`) or `Authorization: Bearer <api token>`. Cross-site
non-GET requests are refused (Origin check). Endpoints that act on one machine take `?host=<id>`
(default `local`).

## Hosts

| Method | Path | |
|---|---|---|
| GET | `/api/hosts` | hosts with reachability, info, session summaries |
| POST | `/api/hosts` | add a host (`label, hostname, port, username` + `password[, store_password]` / `private_key` / `auth: "manual"`) |
| PATCH | `/api/hosts/{hid}` | edit |
| DELETE | `/api/hosts/{hid}` | remove (tmux on the host is not touched) |
| POST | `/api/hosts/{hid}/test` | probe: OS, hostname, tmux, claude |
| GET | `/api/ssh-key` | the console's public key |

## Sessions and windows (`?host=`)

| Method | Path | |
|---|---|---|
| GET | `/api/sessions` (`/api/windows`) | snapshot: saved + live windows (`?refresh=1` polls first) |
| POST | `/api/sessions` | create (`name, display, provider, cwd, command, autostart, start`) |
| PATCH | `/api/sessions/{name}` | edit / rename / adopt an unsaved window |
| DELETE | `/api/sessions/{name}` | stop and forget the record |
| POST | `/api/sessions/{name}/start` | `{resume}` |
| POST | `/api/sessions/{name}/stop` | |
| POST | `/api/sessions/{name}/restart` | `{resume}` — respawn in place if running, else start |
| POST | `/api/sessions/{name}/hide` | `{hidden}` |
| GET | `/api/sessions/{name}/conversations` | `{current, running, cwd, claude, conversations: [{id, mtime, size, title, prompt}]}` |
| POST | `/api/sessions/{name}/conversations/{cid}` | `{action: "resume" \| "fork" \| "delete"}` |
| POST | `/api/shells` | `{cwd?}` — new tmux login shell `shell-N` (not saved) |
| GET | `/api/windows/{wid}/capture` | `?lines=` — screen text |
| POST | `/api/windows/{wid}/action` | `{action: "interrupt" \| "respawn" \| "send", …}` |
| GET | `/api/dirs` | folder suggestions |

## Files and uploads (`?host=`)

| Method | Path | |
|---|---|---|
| GET | `/api/fs/list?path=` | `{path (resolved), entries: [{name, type, size, mtime, target?}], truncated}` |
| GET | `/api/fs/recent?path=&minutes=` | files changed lately under `path` |
| GET | `/api/fs/read?path=[&download=1]` | bytes; header `X-Version: "<size> <mtime>"` |
| PUT | `/api/fs/write?path=[&force=1]` | body = content, header `X-Version` (empty for a new file); 409 `{conflict, version}` |
| POST | `/api/upload?window=&name=` | body = file (≤ 25 MB) → `{path, name, size}` |

Paths are absolute or `~/…` (expanded on the host).

## Other

| Method | Path | |
|---|---|---|
| POST | `/api/login`, `/api/logout` | |
| GET | `/api/whoami` | `{user, host, auth, version, commit}` |
| GET/PUT | `/api/settings` | defaults, token, URLs, secret store info |
| PUT | `/api/env` | set / delete a session environment variable |
| GET/POST | `/api/messages` | team board |
| GET/DELETE | `/api/logs` | this user's event log (`?lines=`) / clear it |
| GET | `/api/events` | Server-Sent Events: `windows`, `hosts`, `message` |

## Terminal WebSocket: `/ws/term`

Query: `host`, `window=@<id>` or `shell=1`, `cols`, `rows`, and for the buffer terminal `mode=buffer&lines=<N>`.

Server → browser: binary frames = terminal output. Text frames:

| Text | Meaning |
|---|---|
| `pong` | answer to `ping` |
| `size:WxH` | (buffer) the pane's size; the browser terminal takes exactly this |

Browser → server (JSON text frames):

| Message | |
|---|---|
| `{t:"in", d}` / `{t:"bin", d: base64}` | input (classic and buffer; buffer passes the bytes exactly) |
| `{t:"resize", c, r}` | this device's size (buffer: applied if this device owns the window size) |
| `{t:"claim"[, c, r]}` | this device is in use: take the window size (not typing) |
| `{t:"select", w}` | show another window in this view |
| `{t:"scroll", n \| page}` | scroll (copy-mode, or wheel events for full-screen programs) |
| `{t:"ping"}` | liveness check |

Close code **1012** = the console is restarting (reconnect at once).
