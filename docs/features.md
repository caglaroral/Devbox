# User guide

## Layout

The sidebar has, from the top: **Hosts**, **Sessions**, **Team**, **Settings**, then the **Hosts & Sessions** tree.
The tree lists every host with its sessions and shells. Hovering an item (always on touch screens) shows two
buttons on its right: a folder (the session's **Files**) and **✕** (close / hide, see below). The bottom-left
corner shows the console version and build commit, e.g. `devbox v0.7.6 · 3f2c1ab`.

## Hosts

- **local** is the machine the console runs on (in the Docker install it is reached over SSH like any other).
- Remote hosts are added on the Hosts page (password once to install the console's key, a stored password,
  a pasted private key, or manual key setup).
- Each host card shows reachability, OS, tmux and claude versions, and buttons for **Shell**, **Test**,
  **Sessions**, **Edit**, **Delete**.

## Sessions

The Sessions page lists **all hosts' sessions, grouped by host**. Each group header has **Shell** and
**+ New session** for that host. Filters (All / Running / Stopped / Hidden), search, **Start all** and refresh
work across all hosts.

A *session* is a saved record (display name, tmux name, folder, command) plus a tmux window running it —
normally `claude`. **+ New session** lets you choose the host; folder suggestions and the default command come
from that host.

Card buttons: folder (**Files**), history (**Conversations**), **✎** edit, **✕** close; **Connect**, or
**Start** / **Resume** when stopped.

### Closing a session (✕)

| Option | What happens |
|---|---|
| Only remove from view | Nothing stops; it moves to the *Hidden* list. |
| Stop | claude and its tmux window close; the card stays. **Resume** continues the same conversation. |
| Delete | claude exits and the card is deleted. Its claude conversations stay in the folder; a new session there can resume them from **Conversations**. The project folder is not touched. |

### Shells

**Shell** opens a login shell in tmux on that host (`shell-1`, `shell-2`, …). Shells are listed with a `$` in the
tree, are not saved (no record, no autostart), and you can come back to them from any device. `exit` (or ✕)
closes them and they disappear from the list.

## Claude conversations

Every saved claude session keeps **its own** conversation:

- A new session (or **New conversation** in the ⋯ menu) starts claude with a fresh `--session-id`.
- **Resume** runs `claude --resume <that id>` — two sessions in the same folder never pick up each other's
  conversation.
- `/clear` and claude's own `/resume` are followed (a SessionStart hook reports the conversation claude is in).

**Conversations** (history button on the card, or ⋯ → *Conversations…* in the terminal) lists the folder's
conversations, newest first, with title or first prompt, age and size, the current one marked:

- **Resume** — switch the session to that conversation (claude restarts in the same window; open terminals stay
  attached).
- **Fork** — continue it as a new conversation; the original stays as it is.
- **Delete** — remove its transcript (not allowed for the conversation a running session is in).

Sessions whose command is not plain `claude` (custom flags) are left alone and can only list conversations.

## Files (per session)

Opened from the folder button (card or tree), **Files** in the terminal header, ⋯ → *Files*, or by clicking a
file path printed in the terminal.

- **Folder** — browse from the session's working folder (breadcrumbs, ⌂ back to it, filter, show dotfiles).
- **Recent** — files changed in the last 24 h under the session folder (`.git`, `node_modules`, venvs and
  caches skipped). The quickest way to see what claude just produced.
- Images, PDFs, video and audio are shown inline (**Open** in a new tab, **Download**).
- Text and code open in a code editor (CodeMirror) with syntax highlighting; **Save** or Ctrl+S.
  If the file changed on disk after you opened it (for example claude edited it), saving asks before
  overwriting; an open file you have not edited reloads by itself when it changes.
- Markdown opens rendered (images next to it are shown); **Edit** shows the source.
- Lists refresh every 5 s.

claude started by the console is told (through `--append-system-prompt`) to print the absolute path of files
you should look at on their own line, so they can be clicked.

## Sending files to a session (📎 Attach)

- **Ctrl+V** a screenshot onto the terminal, **drag and drop** files onto it, or **📎 Attach** (on a phone this
  opens the file / gallery / camera picker).
- Any file type up to 25 MB. It is saved on the session's host in
  `<session folder>/.devbox/uploads/<time>-<original name>` (pasted screenshots: `img-…png`), its path is typed
  into the prompt (not sent) so you can write your message around it.
- `.devbox/` is added to the repository's `.git/info/exclude` (the project's `.gitignore` is not touched).
- Uploads older than 7 days are deleted on the next upload.

## The terminal

- **Buffer terminal** (default): the browser keeps the conversation in its own scrollback buffer, so scrolling
  is local and smooth (GPU rendering via WebGL). Scroll up as far as the buffer goes; **↓ Jump to bottom**
  (or **↓ New output** when something new arrived) brings you back; typing also jumps to the bottom.
- **Classic terminal** (Settings → This device): a tmux client draws the screen; scrolling goes through tmux.
  Use it if something looks wrong in buffer mode.
- One program draws for one size: when two devices show the same session, the one you used last sets the size;
  the other shows *"Sized for a smaller/bigger screen — click/tap to fit here"*. Touching, clicking or typing on
  a device takes the size.
- Header: **Copy**, **Paste**, **📎 Attach**, **Files**, **Text** (screen text for selecting), **A−/A+**, **⋯**.
  On a phone these are in the **⋯** menu.
- **⋯** menu: Send Ctrl‑C, Edit…, Conversations…, Files, Screen text, New conversation, Resume,
  Close…, and a field to send a prompt or slash command.

### Phone

- Tap the terminal for the keyboard; the key bar has Esc, Tab, Ctrl, arrows, Enter, ^C, ⇧Tab, PgUp/PgDn,
  Select, Paste, 📎, ✎ Type, ⋯.
- Swipe to scroll, flick for momentum, pinch to change the font size, long-press for selectable screen text.
- The phone keyboard's autocorrect does not cause erase-and-retype at spaces (the console sends only the real
  difference). **✎ Type** opens a normal text box (autocorrect, dictation) and sends it at once.

## Settings

- **This device** (stored in this browser only): terminal buffer size (1,000–100,000 lines, default 10,000) and
  the **Classic terminal** switch.
- API keys / environment variables for sessions (kept in the secret store, never shown again).
- New session defaults (command, projects root used for folder suggestions).
- Team board access, secret store, console SSH key.
- **Event log**: terminal connects/disconnects (who closed it, after how long, from which device), host
  reachability, sessions, conversations, uploads, file saves, logins and console restarts. Kept 7 days;
  **Clear log** deletes it.

## Updates

When the console is redeployed, open terminals show *"Console is restarting (update) — your session keeps
running"* and reconnect within seconds (sessions keep running in tmux). If the version changed, a bar offers
**Reload** so the page runs the new code.
