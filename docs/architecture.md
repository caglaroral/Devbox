# Architecture

## Overview

```
browser (xterm.js + WebGL) ──WebSocket /ws/term──► console (aiohttp, app.py) ──► tmux on the host
        └── HTTP /api/*, SSE /api/events ─────────►        │                   (local or over SSH)
                                                            └── Vault / file secret store
```

- **console** — one Python process (`devbox_console/app.py`, aiohttp). In the Docker install every host,
  including "local", is reached over SSH; SSH calls share a ControlMaster connection.
- **Box** = one machine of one user: tmux polling (`list-windows` + `capture-pane` every `DEVBOX_POLL` s),
  saved sessions (`<data>/users/<user>/boxes/<host>/sessions.json`), status classification, snapshots.
- **User** = settings, SSH keys, hosts, event log, boxes.
- Saved sessions live in tmux session `main`; each browser terminal gets its own grouped tmux session
  (`web-xxxxxx`, `destroy-unattached on`) so every tab can look at a different window.

## Terminal modes

### Classic

A PTY runs a normal tmux client (`tmux new-session -t =main -s web-…`). tmux draws the screen; the browser shows
it. Scrolling a normal pane uses tmux copy-mode; full-screen programs get wheel events.

### Buffer (default)

The browser keeps the scrollback itself:

- A **tmux control-mode client** (`tmux -C new-session -t =main -s web-…`, no TTY) streams the pane's raw
  output byte for byte as `%output %<pane> <octal-escaped data>`. A redrawing tmux client would only send the
  final screen when output scrolls quickly (lines lost); control mode doesn't.
- Input goes in with `send-keys -t %<pane> -H <hex bytes>` — exact bytes, including UTF-8 and escape sequences.
- On connect and on window switch the pane is **replayed** from `capture-pane`: history (`-S -N -E -1 -J`),
  screen, cursor position, and modes (alternate screen, mouse tracking, keypad, cursor visibility, bracketed
  paste). tmux exposes no bracketed-paste flag, so it is taken from the output stream (`\e[?2004h/l`) and
  defaults to on for claude and common shells.
- New tmux windows get `history-limit 50000` so a reconnect can refill the buffer.
- claude started by the console runs with `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` (its inline renderer):
  the conversation flows into the scrollback instead of being redrawn on the alternate screen. Sessions started
  before that need one **Resume**.
- Rendering uses the WebGL addon (`@xterm/addon-webgl`); on GPU context loss xterm falls back to the DOM
  renderer.

### Window size

A program draws for one size, so a window has one size even when several devices show it.

- Buffer mode: the server tells the browser the pane size (`size:WxH`) and the browser terminal takes exactly
  that. A device that opens or *claims* a window sets it with `resize-window` (this makes the window's
  `window-size` manual; a classic client resets it with `set-option -wu window-size` when it claims).
- Classic mode: tmux `window-size latest`; a claim is a focus-in report (`\e[I`) through the client, which makes
  it tmux's "latest" client without reaching the program.
- A device claims when it is actually used: focus, click/tap, mouse move/wheel over the terminal, typing,
  visibility change. There is no timer (two open screens would fight). The other device shows a
  "Sized for … — click to fit here" bar.
- Experiments that were reverted (v0.7.3 font fitting, v0.7.4 handoff panel) are described in the changelog.

## Scrolling

- Buffer mode, normal screen: touch and PgUp/PgDn scroll the browser's own buffer (`term.scrollLines`), with
  momentum after a flick; the mouse wheel is handled by xterm.js. **↓ Jump to bottom** appears when scrolled up.
- Full-screen programs (alternate screen, no scrollback) or classic mode: the server sends **wheel events**
  when the program asked for mouse input — written straight into the view's tmux client (one byte string, no
  extra command per step); otherwise tmux copy-mode scrolls. Claude Code scrolls one line per wheel event.
- Copy-mode belongs to the pane, not the device. The box remembers which view scrolled a window into
  copy-mode; typing from any device leaves it first (otherwise keys go to copy-mode), and a claim from another
  device does too, while the scrolling device's own claims keep it.

## Input on phones

Phone keyboards compose words and re-insert the composed word at every space; xterm.js treats that as a
replacement and sends erase + retype. On touch screens the console handles xterm's hidden textarea itself
(`imeDiff`): it compares the field before and after each change and sends only the difference — letters as
typed, nothing extra at the space, only the changed letters for a real correction. A zero-width marker keeps
the field from being empty so backspace always arrives; Enter sends CR; Esc, Tab, arrows and Ctrl+… still go
through xterm. Autocorrect/autocapitalize are switched off on the textarea; **✎ Type** keeps them.

## Claude conversation tracking

- Claude Code keeps transcripts in `~/.claude/projects/<folder with non-alphanumerics as ->/<id>.jsonl`.
- For a session whose command is plain `claude`, the console builds the command (`Box.build_claude`):
  `--resume <id>` when the session's conversation exists, else `--session-id <new uuid>` (records without an id
  fall back to `--continue`), plus `--settings` with a **SessionStart hook** and `--append-system-prompt`.
- The hook (only for that launch; `~/.claude` is not touched) writes claude's hook JSON to
  `~/.devbox/claude/<session key>.json` on the host (`DEVBOX_SESSION_KEY` is set on the window). It fires on
  startup, resume, `/clear` and compaction, so the session record follows the conversation claude is actually
  in.
- Resume/Fork of another conversation respawns the pane in place (`respawn-pane -k`), so open terminals stay
  attached.
- Commands with their own conversation flags (`-c`, `-r`, `--session-id`, …) are not modified.

## Files and uploads

- All file operations run on the session's host as the console user (no more than its shell can do), through
  small POSIX shell scripts (`FS_*_SH`) so they work locally and over SSH; `~/` is expanded on the host.
- Saves carry a version token `"<size> <mtime>"`; a mismatch returns 409 (overwrite with `force=1`). Writes are
  in place, so file mode and owner are kept.
- SVG and text responses carry a sandbox CSP (never run as a page of the console); PDFs do not (Chrome refuses
  sandboxed PDFs).
- Uploads stream into `<pane folder>/.devbox/uploads/` (the pane's current path, else its start path, else
  `$HOME`), names are made safe (no paths, no leading dots).
- The editor (CodeMirror 5, per-language modes) and markdown preview (marked + DOMPurify) are lazy-loaded from
  cdnjs.

## Robustness

- **Restarts**: on shutdown the console closes open terminals with WebSocket code **1012**; browsers show
  "restarting" and retry every second; after reconnecting the page compares versions and offers **Reload**.
- **Dead sockets** (phone slept, laptop resumed): when the tab becomes visible or the network returns the
  browser pings; no `pong` within 2.5 s → reconnect at once.
- **Instantly failing commands**: `remain-on-exit` is set in the same tmux call as `new-window`, so a command
  that fails at once stays visible as a dead window with its error instead of vanishing (and, as the last
  window, taking the tmux server with it).
- **claude in `~/.local/bin`**: session commands and the host probe put `~/.local/bin` first in `PATH`
  (Debian's root profile does not include it).
- **Console SSH key after a restart**: the runtime dir is a tmpfs; the stored key is written back when the user
  is loaded, before the pollers start.

## Event log

Per user, `<data>/users/<name>/logs/YYYYMMDD.log`, one line per event (`term.open/close` with `by=browser|
process|restart`, duration, device, mode; `host.unreachable/ok`; `session.*`; `conversation.*`; `shell.open`;
`upload`; `file.save`; `login`; `console.start` with version and commit). Files older than `DEVBOX_LOG_DAYS`
(7) are deleted; Settings shows and clears it.
