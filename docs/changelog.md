# Changelog

## v0.7.15
- Desktop terminal header without Copy / Paste / Text (Ctrl+C / Ctrl+V; screen text is in the ⋯ menu).
  Phones keep Copy / Paste in the ⋯ menu and key bar.

## v0.7.14
- **Ctrl+C copies** the selected text (without a selection it is still the interrupt); Ctrl+V pastes.

## v0.7.13
- Terminal ANSI colours = GNOME Terminal's default palette (mc and other programs look like they do locally).

## v0.7.12
- Fix: the host editor opened on *Manual*, so saving a host with a stored password or key (even just a
  new label) deleted that credential. It now opens on the current method, and switching to Manual asks first.

## v0.7.11
- Phone ⋯ menu: buttons stay inside the dialog (equal columns; Copy / Paste / Attach and A− / A+ rows);
  "Restart (new conversation)" is now **New conversation**.

## v0.7.10
- Phone: tapping **↓ Jump to bottom** no longer opens the keyboard.

## v0.7.9
- Name shown as **Devbox** (sidebar, browser tab; the tab shows `<session> · Devbox` in a terminal).

## v0.7.8
- ☰ menu icon stretched horizontally (button frame as before v0.7.7).

## v0.7.7
- Wider ☰ menu button on phones.

## v0.7.6
- **↓ Jump to bottom** in the terminal when scrolled up in the buffer (**↓ New output** when something new
  arrived meanwhile) — claude's full-screen mode used to show one.
- Documentation in `docs/`.

## v0.7.5
- Back to the v0.7.2 behaviour: reverts v0.7.3 and v0.7.4 (code identical to v0.7.2 apart from the version).

## v0.7.4 (reverted)
- Handoff panel ("Live on your other device — Continue here") on the device without the window size.

## v0.7.3 (reverted)
- The device without the window size fitted it by changing the font size; `size:WxH:own` ownership flag.

## v0.7.2
- Fix: the Resume / Start buttons in the terminal's "stopped" panel got no clicks (xterm's own layers sat
  above the panel). The stopped panel also replaces a stale "Connecting…" / "Session not found".

## v0.7.1
- WebGL terminal renderer; longer, native-like glide when scrolling locally.

## v0.7.0
- **Buffer terminal**: tmux control mode streams raw output, the browser keeps the scrollback; history replayed
  on connect; exact input via `send-keys -H`; pane size from the server.
- claude runs with `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` (inline renderer).
- Settings → This device: terminal buffer size and Classic terminal switch (replace the scroll speed).
- Files button next to ✕ in the sidebar tree.

## v0.6.x — mobile and files
- 0.6.8 touch scroll speed setting (replaced in 0.7.0).
- 0.6.7 own handling of phone keyboard input (no erase + retype at spaces); scroll gain.
- 0.6.6 autocorrect off in the terminal; shorter glide.
- 0.6.5 wheel events straight into the tmux client (no command per swipe step); touch momentum.
- 0.6.4 full-screen programs (claude) get wheel events instead of copy-mode (`[0/0]`).
- 0.6.3 size claims are not typing (fix for 0.6.2 breaking phone scrolling).
- 0.6.2 typing leaves copy-mode left behind by another device.
- 0.6.1 Files: normal filter box; card icons grouped on the right.
- 0.6.0 **Files panel** per session (browse, recent, view, edit with CodeMirror, markdown preview); clickable
  file paths in the terminal; `--append-system-prompt` hint for claude.

## v0.5.x — conversations and polish
- 0.5.5 restart-aware terminals (close code 1012, fast reconnect) and "updated — reload" bar.
- 0.5.4 sidebar menu as buttons (a browser setting underlined links).
- 0.5.3 theme-matching history icon; plain sidebar counts.
- 0.5.2 ✕ on tree items; clearer close dialog; "Hosts & Sessions".
- 0.5.1 claude found in `~/.local/bin`; instantly failing commands stay visible.
- 0.5.0 **claude conversations per session** (pinned ids, SessionStart hook, Conversations dialog with
  Resume / Fork / Delete); size bar; mobile ⋯ menu.

## v0.4.0
- Shells live in tmux and are listed in the tree; one "Shell" name for local and remote hosts.

## v0.3.x
- 0.3.1 attach any file type; console SSH key restored at startup; log times in local time.
- 0.3.0 event log; dead-socket detection; reclaim the terminal size on real use.

## v0.2.0
- Sessions grouped by host, host picker for new sessions, image upload, version display.
