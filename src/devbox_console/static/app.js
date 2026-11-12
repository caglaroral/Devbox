"use strict";
/* devbox console — client. No build step; xterm.js from CDN.
 * A "session" = saved record (name, display, provider, start folder, command) + tmux window running claude.
 * Hosts: "local" (this machine) + remote machines over SSH; every host has its own sessions.
 * Routes: #/sessions (all hosts, grouped)  #/h/<host>/sessions  #/h/<host>/term/<name>  #/h/<host>/shell  #/hosts  #/team  #/settings
 */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isMobile = () => matchMedia("(max-width: 820px)").matches;
const isTouch = () => matchMedia("(pointer: coarse)").matches;
const isTermRoute = () => S.route === "term" || S.route === "shell";

const S = {
  windows: [], session: "main", host: "", user: "", error: null,
  hostId: "local", hosts: [], login: "",
  current: null,          // session name shown in the terminal
  route: "sessions", filter: "all",
  messages: [], lastMsgId: 0, unread: 0,
  settings: null,
  byHost: {},             // host id -> last snapshot (the Sessions page lists every host)
};

// line icon in the text colour (emoji don't match the theme)
const ICON_HISTORY = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.6 8a5.4 5.4 0 1 0 1.5-3.8"/><path d="M2.4 2.6v2.6H5"/><path d="M8 5.2V8l2 1.4"/></svg>';
const STATUS_LABEL = { working: "Working", waiting: "Waiting for approval", idle: "Idle", shell: "Shell", dead: "Exited", stopped: "Stopped" };
const cur = () => S.windows.find((w) => w.name === S.current);
const hp = (p) => `#/h/${encodeURIComponent(S.hostId)}/${p}`;
const curHost = () => S.hosts.find((h) => h.id === S.hostId);
// endpoints that act on one host get ?host=<id>
const HOST_SCOPED = /^\/(api\/(sessions|shells|windows|dirs|settings|events|upload)|ws\/term)/;
function withHost(path, host = S.hostId) {
  if (!HOST_SCOPED.test(path)) return path;
  return path + (path.includes("?") ? "&" : "?") + "host=" + encodeURIComponent(host);
}

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const r = await fetch(withHost(path, opts.host || S.hostId), {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
const sessApi = (name, sub = "", opts = {}) => api(`/api/sessions/${encodeURIComponent(name)}${sub}`, opts);
const termHash = (host, name) => `#/h/${encodeURIComponent(host)}/term/${encodeURIComponent(name)}`;
const hostWindows = (hid) => (hid === S.hostId ? S.windows : S.byHost[hid]?.windows || []);
const findWin = (name, hid = S.hostId) => hostWindows(hid).find((x) => x.name === name);
const hostLabel = (hid) => S.hosts.find((h) => h.id === hid)?.label || (hid === "local" ? S.host : hid);

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 2400);
}

/* ------------------------------------------------------------------ clipboard */
async function copyText(text) {
  if (!text) { toast("Nothing selected"); return false; }
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // plain http on a LAN address: Clipboard API is unavailable, fall back to execCommand.
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy"); ta.remove();
    if (!ok) { openTextDialog(text, "Could not copy — select and copy from here"); return false; }
  }
  toast(`Copied (${text.length} characters)`);
  return true;
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { term.paste(text); if (!isTouch()) term.focus(); return; }
  } catch { /* permission denied or insecure context */ }
  openPasteDialog("Paste", "Could not read the clipboard. Paste the text here (long-press → Paste) and tap Send.");
}

/* ------------------------------------------------------------------ terminal */
const FONT_KEY = "devbox.fontSize";
let fontSize = +(localStorage.getItem(FONT_KEY) || (isMobile() ? 11 : 13));

const term = new Terminal({
  fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", "DejaVu Sans Mono", monospace',
  fontSize, cursorBlink: true, allowProposedApi: true, scrollback: 2000,
  macOptionIsMeta: true, rightClickSelectsWord: false,
  theme: { // GitHub dark
    background: "#0d1117", foreground: "#e6edf3", cursor: "#2f81f7", cursorAccent: "#0d1117",
    selectionBackground: "rgba(56,139,253,0.4)",
    black: "#484f58", red: "#ff7b72", green: "#3fb950", yellow: "#d29922", blue: "#58a6ff", magenta: "#bc8cff", cyan: "#39c5cf", white: "#b1bac4",
    brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#56d364", brightYellow: "#e3b341", brightBlue: "#79c0ff", brightMagenta: "#d2a8ff", brightCyan: "#56d4dd", brightWhite: "#ffffff",
  },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.loadAddon(new WebLinksAddon.WebLinksAddon());
try { term.loadAddon(new Unicode11Addon.Unicode11Addon()); term.unicode.activeVersion = "11"; } catch {}
term.open($("#term"));

// OSC 52: tmux copy-mode (set-clipboard) and many TUIs push copied text this way.
term.parser.registerOscHandler(52, (data) => {
  const b64 = data.slice(data.indexOf(";") + 1);
  if (!b64 || b64 === "?") return true;
  try { copyText(new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)))); } catch {}
  return true;
});

// Ctrl+Shift+C (and Cmd+C on mac) copies. Ctrl+V / Ctrl+Shift+V / Cmd+V are left to the browser: its native
// paste event works on plain http too and also carries images (see the paste handler below).
term.attachCustomKeyEventHandler((e) => {
  if (e.type !== "keydown") return true;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey && e.shiftKey && k === "c") || (e.metaKey && k === "c" && term.hasSelection())) {
    copyText(term.getSelection()); e.preventDefault(); return false;
  }
  if (k === "v" && ((e.ctrlKey && !e.altKey) || e.metaKey)) return false; // no ^V; the browser fires "paste"
  return true;
});

let ws = null, wsKind = null, wsSess = null, wsWanted = false, wsRetry = 0, ctrlArmed = false;
function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
function overlay(msg, html = false) {
  const o = $("#termOverlay");
  if (!msg) return o.classList.add("hidden");
  if (html) o.innerHTML = msg; else o.textContent = msg;
  o.classList.remove("hidden");
}
function closeWs() { wsWanted = false; if (ws) { ws.onclose = null; ws.close(); ws = null; } }

/* tmux runs with window-size=latest: a shared window takes the size of the client used last. When the phone
   attaches, the window shrinks and the desktop tab shows dots around a small screen until you type there.
   A focus-in report (ESC [ I) makes this tab the "latest" client again without reaching the program,
   so send it whenever this device is actually in use.
   Only real use of this device claims it (no timer): with two screens open a timer would make them fight. */
let lastClaim = 0;
function claimSize(gap = 500) {
  if (wsKind !== "tmux" || !ws || ws.readyState !== 1 || document.hidden) return;
  const now = Date.now(); if (now - lastClaim < (typeof gap === "number" ? gap : 500)) return;
  lastClaim = now;
  wsSend({ t: "in", d: "\x1b[I" });
  $("#sizeBar")?.classList.add("hidden"); // the next snapshot shows the new size
}
window.addEventListener("focus", () => claimSize());

// One program has one size: when another device was used last, say so (instead of a half-empty screen)
// and let a click/tap take the size back.
function updateSizeBar() {
  const bar = $("#sizeBar"), w = S.route === "term" ? cur() : null;
  const off = w?.running && w.width && ws && wsKind === "tmux" && (w.width !== term.cols || Math.abs(w.height - term.rows) > 1);
  bar.classList.toggle("hidden", !off);
  if (!off) return;
  const smaller = w.width < term.cols;
  bar.innerHTML = `Sized for ${smaller ? "a smaller" : "a bigger"} screen (<b>${w.width}×${w.height}</b>) — ${isTouch() ? "tap" : "click"} to fit here`;
}
$("#sizeBar").addEventListener("click", (e) => { e.stopPropagation(); claimSize(0); $("#sizeBar").classList.add("hidden"); });
$("#termWrap").addEventListener("pointerdown", () => claimSize(), true);
$("#termWrap").addEventListener("mouseenter", () => claimSize());
$("#termWrap").addEventListener("mousemove", () => claimSize(2000), { passive: true }); // back at the desk
$("#termWrap").addEventListener("wheel", () => claimSize(2000), { passive: true, capture: true });
$("#termWrap").addEventListener("touchstart", () => claimSize(), { passive: true, capture: true });
term.textarea?.addEventListener("focus", () => claimSize());

/* A socket can look open while it is dead (phone slept, laptop resumed): the terminal just freezes until the
   server's heartbeat gives up. When the tab comes back, ping it and reconnect at once if there is no answer. */
let pongTimer = null;
function checkAlive() {
  if (!ws || ws.readyState !== 1 || pongTimer) return;
  const sock = ws;
  wsSend({ t: "ping" });
  pongTimer = setTimeout(() => {
    pongTimer = null;
    if (ws !== sock) return;
    ws.onclose = null; try { ws.close(); } catch {}
    ws = null; wsRetry = 0;
    if (wsWanted && isTermRoute()) connect();
  }, 2500);
}

function connect() {
  if (ws && ws.readyState <= 1) return;
  const shell = S.route === "shell";
  const w = cur();
  if (!shell && (!w || !w.running)) { closeWs(); showStopped(w); return; }
  wsWanted = true;
  fit.fit();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ cols: term.cols, rows: term.rows, ...(shell ? { shell: "1" } : { window: w.id }) });
  wsKind = shell ? "shell" : "tmux";
  wsSess = shell ? null : w.tmux_session;
  overlay("Connecting…");
  ws = new WebSocket(`${proto}://${location.host}${withHost(`/ws/term?${q}`)}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { wsRetry = 0; overlay(null); term.reset(); wsSend({ t: "resize", c: term.cols, r: term.rows }); lastClaim = 0; setTimeout(claimSize, 300); if (!isTouch()) term.focus(); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") { if (ev.data === "pong") { clearTimeout(pongTimer); pongTimer = null; } return; }
    term.write(new Uint8Array(ev.data));
  };
  ws.onclose = () => {
    ws = null;
    if (!wsWanted) return;
    const delay = Math.min(1000 * 2 ** wsRetry++, 15000);
    overlay(`Disconnected — retrying in ${Math.round(delay / 1000)} s`);
    setTimeout(() => { if (wsWanted && isTermRoute()) connect(); }, delay);
  };
}

function showStopped(w) {
  term.reset();
  if (!w) return overlay("Session not found");
  overlay(`<div class="stopped-box"><b>${esc(w.display)}</b><span>This session is stopped. Folder: <code>${esc(shortPath(w.cwd))}</code></span>
    <div><button class="btn primary" data-ov="resume">Resume (--continue)</button> <button class="btn" data-ov="fresh">Start fresh</button></div></div>`, true);
}
$("#termOverlay").addEventListener("click", async (e) => {
  const a = e.target.dataset?.ov; if (!a) return;
  await startSession(S.current, a === "resume");
});

term.onData((d) => {
  if (ctrlArmed && d.length === 1) {
    const c = d.toUpperCase().charCodeAt(0);
    if (c >= 64 && c <= 95) d = String.fromCharCode(c - 64);
    setCtrl(false);
  }
  wsSend({ t: "in", d });
});
term.onBinary((d) => wsSend({ t: "bin", d: btoa(d) }));

let resizeTimer;
new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!isTermRoute()) return;
    fit.fit(); wsSend({ t: "resize", c: term.cols, r: term.rows });
    updateSizeBar();
  }, 80);
}).observe($("#termWrap"));

function setFont(n) {
  fontSize = Math.max(8, Math.min(24, n));
  localStorage.setItem(FONT_KEY, fontSize);
  term.options.fontSize = fontSize;
  fit.fit(); wsSend({ t: "resize", c: term.cols, r: term.rows });
}
$("#fontMinus").onclick = () => setFont(fontSize - 1);
$("#fontPlus").onclick = () => setFont(fontSize + 1);
$("#copyBtn").onclick = () => (term.hasSelection() ? copyText(term.getSelection()) : openScreenText());
$("#pasteBtn").onclick = pasteFromClipboard;
$("#textBtn").onclick = () => openScreenText();

/* touch: swipe = scroll history (tmux copy-mode), pinch = font size, long-press = selection view, tap = keyboard */
(() => {
  const el = $("#termWrap");
  let y0 = null, acc = 0, pinch0 = null, f0 = fontSize, moved = false, lp = null, longPressed = false;
  const cancelLP = () => { clearTimeout(lp); lp = null; };
  const lineH = () => fontSize * 1.2;
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  el.addEventListener("touchstart", (e) => {
    if (e.target.closest(".stopped-box")) return;
    moved = false;
    if (e.touches.length === 2) { pinch0 = dist(e.touches); f0 = fontSize; y0 = null; }
    else if (e.touches.length === 1) {
      y0 = e.touches[0].clientY; acc = 0; longPressed = false;
      lp = setTimeout(() => { longPressed = true; navigator.vibrate?.(15); openScreenText(); }, 550);
    }
    if (e.touches.length !== 1) cancelLP();
  }, { passive: true });
  el.addEventListener("touchmove", (e) => {
    if (pinch0 && e.touches.length === 2) {
      const n = Math.max(8, Math.min(24, Math.round(f0 * dist(e.touches) / pinch0)));
      if (n !== fontSize) { fontSize = n; term.options.fontSize = n; }
      e.preventDefault(); return;
    }
    if (y0 == null) return;
    const y = e.touches[0].clientY;
    acc += y - y0; y0 = y;
    if (Math.abs(acc) > 8) cancelLP();
    const n = Math.trunc(acc / lineH());
    if (n) { moved = true; acc -= n * lineH(); if (S.route === "shell") term.scrollLines(-n); else wsSend({ t: "scroll", n }); }
    e.preventDefault();
  }, { passive: false, capture: true });
  el.addEventListener("touchend", (e) => {
    if (e.target.closest(".stopped-box")) return;
    cancelLP();
    if (pinch0) { pinch0 = null; setFont(fontSize); }
    y0 = null;
    if (!moved && !longPressed) term.focus(); // a tap opens the keyboard
  }, { passive: true });
  el.addEventListener("contextmenu", (e) => { if (isTouch()) e.preventDefault(); });
})();

/* files: paste / drop / 📎 → saved into <session folder>/.devbox/uploads on its host, path typed into the prompt */
const UPLOAD_MAX = 25 * 1024 * 1024;
const realFiles = (list) => [...(list || [])].filter(Boolean);
async function sendFiles(files) {
  if (!files.length) return;
  if (!isTermRoute() || (S.route === "term" && !cur()?.running)) return toast("Open a running session first");
  const w = S.route === "term" ? cur() : null, paths = [];
  for (const f of files) {
    if (f.size > UPLOAD_MAX) { toast(`${f.name || "file"}: larger than 25 MB`); continue; }
    toast(`Sending ${f.name || "file"} (${Math.ceil(f.size / 1024)} KB)…`);
    try {
      const q = "?" + new URLSearchParams({ ...(w ? { window: w.id } : {}), ...(f.name && f.name !== "image.png" ? { name: f.name } : {}) });
      const r = await fetch(withHost(`/api/upload${q}`), { method: "POST", body: f, headers: { "Content-Type": f.type || "application/octet-stream" } });
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) throw new Error((await r.text()) || r.statusText);
      paths.push((await r.json()).path);
    } catch (e) { toast(`Upload failed: ${e.message}`); }
  }
  if (!paths.length) return;
  const quote = (p) => (/^[\w@%+=:,./~-]+$/.test(p) ? p : `'${p.replace(/'/g, "'\\''")}'`);
  term.paste(paths.map(quote).join(" ") + " "); // like dropping files on a local terminal: claude attaches them
  toast(paths.length === 1 ? `Sent: ${paths[0].split("/").slice(-3).join("/")}` : `${paths.length} files sent`);
  if (!isTouch()) term.focus();
}
$("#termWrap").addEventListener("paste", (e) => {
  const files = realFiles([...(e.clipboardData?.items || [])].filter((i) => i.kind === "file").map((i) => i.getAsFile()));
  if (!files.length) return; // plain text: xterm pastes it
  e.preventDefault(); e.stopPropagation();
  sendFiles(files);
}, true);
(() => {
  const el = $("#termWrap");
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  el.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; depth++; el.classList.add("dropping"); e.preventDefault(); });
  el.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  el.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; el.classList.remove("dropping"); } });
  el.addEventListener("drop", (e) => {
    depth = 0; el.classList.remove("dropping");
    if (!hasFiles(e)) return;
    e.preventDefault();
    const files = realFiles(e.dataTransfer.files);
    if (files.length) sendFiles(files);
  });
})();
const pickFiles = () => { $("#attachInput").value = ""; $("#attachInput").click(); };
$("#attachInput").addEventListener("change", (e) => sendFiles(realFiles(e.target.files)));
$("#attachBtn").onclick = pickFiles;

/* keybar */
const KEYS = { esc: "\x1b", tab: "\t", shifttab: "\x1b[Z", up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D", enter: "\r", ctrlc: "\x03" };
function setCtrl(on) { ctrlArmed = on; $("#ctrlKey").classList.toggle("on", on); }
$("#keybar").addEventListener("pointerdown", (e) => e.preventDefault()); // keep the soft keyboard open
$("#keybar").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.mod === "ctrl") return setCtrl(!ctrlArmed);
  const k = b.dataset.key, a = b.dataset.action;
  if (k === "pgup") return S.route === "shell" ? term.scrollPages(-1) : wsSend({ t: "scroll", page: 1 });
  if (k === "pgdn") return S.route === "shell" ? term.scrollPages(1) : wsSend({ t: "scroll", page: -1 });
  if (k) return wsSend({ t: "in", d: KEYS[k] });
  if (a === "select") return term.hasSelection() ? copyText(term.getSelection()) : openScreenText();
  if (a === "paste") return pasteFromClipboard();
  if (a === "attach") return pickFiles();
  if (a === "manage") return openManage();
  if (a === "compose") return openPasteDialog("Type", "Comfortable typing with autocorrect and dictation. Send passes it to the terminal.", true);
});

/* selection view / paste dialogs */
let textLines = 300;
function openTextDialog(text, title = "Screen text") {
  $("#textDlg h2").textContent = title;
  const ta = $("#textArea"); ta.value = text;
  if (!$("#textDlg").open) $("#textDlg").showModal();
  ta.blur(); // no soft keyboard; a readonly textarea still allows native selection handles
  requestAnimationFrame(() => (ta.scrollTop = ta.scrollHeight));
}
function xtermBufferText(maxLines) {
  const b = term.buffer.active, out = [];
  for (let i = Math.max(0, b.length - maxLines); i < b.length; i++) {
    const line = b.getLine(i); if (!line) continue;
    if (line.isWrapped && out.length) out[out.length - 1] += line.translateToString(true);
    else out.push(line.translateToString(true));
  }
  return out.join("\n").replace(/\s+$/, "") + "\n";
}
async function openScreenText(more = false) {
  textLines = more ? Math.min(textLines * 4, 5000) : 300;
  try {
    const w = cur();
    let text;
    if (S.route === "shell") text = xtermBufferText(textLines);
    else if (w?.running) text = (await api(`/api/windows/${encodeURIComponent(w.id)}/capture?lines=${textLines}`)).text;
    else return;
    openTextDialog(text);
  } catch (e) { toast(e.message); }
}
const taSelection = () => { const ta = $("#textArea"); return ta.value.slice(ta.selectionStart, ta.selectionEnd); };
$("#textClose").onclick = () => $("#textDlg").close();
$("#textCopyAll").onclick = () => copyText($("#textArea").value);
$("#textCopySel").onclick = () => { const t = taSelection() || String(getSelection() || ""); t ? copyText(t) : toast("Select some text first"); };
$("#textPasteSel").onclick = () => {
  const t = taSelection() || String(getSelection() || "");
  if (!t) return toast("Select some text first");
  term.paste(t); $("#textDlg").close(); toast("Typed into the terminal");
};
$("#textMore").onclick = () => openScreenText(true);
$$(".text-actions .btn").forEach((b) => b.addEventListener("pointerdown", (e) => e.preventDefault())); // keep selection

function openPasteDialog(title, hint, enter = false) {
  $("#pasteTitle").textContent = title; $("#pasteHint").textContent = hint;
  $("#pasteArea").value = ""; $("#pasteEnter").checked = enter;
  $("#pasteDlg").showModal(); $("#pasteArea").focus();
}
$("#pasteDlg").addEventListener("close", () => {
  if ($("#pasteDlg").returnValue !== "ok") return;
  const text = $("#pasteArea").value;
  if (text) term.paste(text);
  if ($("#pasteEnter").checked) setTimeout(() => wsSend({ t: "in", d: "\r" }), 120);
  if (!isTouch()) term.focus();
});
$("#pasteArea").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#pasteDlg").close("ok"); }
});

/* ------------------------------------------------------------------ session actions */
function applySnapshot(snap) {
  if (snap.host_id) S.byHost[snap.host_id] = snap;
  if (snap.host_id && snap.host_id !== S.hostId) { renderCards(); renderSidebar(); return; }
  Object.assign(S, { windows: snap.windows, session: snap.session, host: snap.host, user: snap.user || S.user,
    login: snap.login || snap.user, error: snap.error });
  renderAll();
  // the session we look at was started/stopped elsewhere: (re)connect or show the stopped panel
  if (S.route === "term") {
    const w = cur();
    // the window we look at is gone (e.g. `exit` in a shell): leave instead of showing another tmux window.
    // Only once it was seen: a late snapshot from before the window existed must not count.
    if (w) applySnapshot.seen = `${S.hostId}/${S.current}`;
    else if (S.current && applySnapshot.seen === `${S.hostId}/${S.current}`) {
      applySnapshot.seen = null; closeWs(); toast(`${S.current} closed`); go("#/sessions"); return;
    }
    if (w?.running && (!ws || wsKind !== "tmux")) { overlay(null); connect(); }
    else if (w && !w.running && ws) { closeWs(); showStopped(w); }
  }
}

async function startSession(name, resume = true, host = S.hostId) {
  try {
    applySnapshot(await sessApi(name, "/start", { method: "POST", body: { resume }, host }));
    toast(resume ? "Resuming (--continue)" : "Started");
    go(termHash(host, name));
  } catch (e) { toast(e.message); }
}
async function stopSession(name, confirmed = false, host = S.hostId) {
  if (!confirmed && !confirm(`Stop "${name}"? claude exits, the saved session stays; you can resume it later.`)) return false;
  try { applySnapshot(await sessApi(name, "/stop", { method: "POST", host })); toast("Stopped"); return true; } catch (e) { toast(e.message); return false; }
}
async function deleteSession(name, confirmed = false, host = S.hostId) {
  if (!confirmed && !confirm(`Delete "${name}"? It is stopped if running and the saved session is deleted. (The project folder is not touched.)`)) return false;
  try {
    applySnapshot(await sessApi(name, "", { method: "DELETE", host })); toast("Deleted");
    if (S.current === name && S.route === "term" && host === S.hostId) go("#/sessions");
    return true;
  } catch (e) { toast(e.message); return false; }
}
function openSession(name, host = S.hostId) {
  if (host === S.hostId && !findWin(name, host)) return;
  go(termHash(host, name));
}

// restart claude of a saved session: resume=true → its own conversation, false → a new one
async function restartSession(name, resume = true, host = S.hostId) {
  try {
    applySnapshot(await sessApi(name, "/restart", { method: "POST", body: { resume }, host }));
    toast(resume ? "Resumed" : "New conversation started");
    if (S.route !== "term" || S.current !== name) go(termHash(host, name));
  } catch (e) { toast(e.message); }
}

/* claude conversations of a session (transcripts in its folder): resume one, fork one, delete one */
let cv = { name: null, host: null };
async function openConversations(name, host = S.hostId) {
  cv = { name, host };
  const w = findWin(name, host);
  $("#cvTitle").textContent = `Conversations · ${w?.display || name}`;
  $("#cvInfo").textContent = "Loading…"; $("#cvList").innerHTML = "";
  if (!$("#convDlg").open) $("#convDlg").showModal();
  try {
    const r = await sessApi(name, "/conversations", { host });
    if (cv.name !== name) return;
    $("#cvInfo").textContent = `Claude conversations in ${shortPath(r.cwd)} on ${hostLabel(host)}, newest first.` +
      (r.claude ? " Resume switches this session to that conversation (claude restarts); Fork continues it as a new one." :
        " This session does not run plain `claude`, so it can only list them.");
    const kb = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`);
    $("#cvList").innerHTML = r.conversations.map((c) => {
      const cur = c.id === r.current;
      return `<li class="${cur ? "cur" : ""}" data-cid="${esc(c.id)}">
        <div class="cv-main"><b>${esc(c.title || c.prompt || "(no prompt)")}</b>
          ${c.title && c.prompt ? `<span class="muted small">${esc(c.prompt)}</span>` : ""}
          <span class="cv-meta">${cur ? "● current · " : ""}${esc(ago(c.mtime))} · ${kb(c.size)} · ${esc(c.id.slice(0, 8))}</span></div>
        <div class="cv-act">${r.claude ? `${cur && r.running ? "" : `<button class="btn primary" data-cv="resume">Resume</button>`}
          <button class="btn" data-cv="fork" title="Continue as a new conversation (the original stays as it is)">Fork</button>` : ""}
          ${cur && r.running ? "" : `<button class="btn danger" data-cv="delete">Delete</button>`}</div>
      </li>`;
    }).join("") || `<li class="muted">No conversations in this folder yet.</li>`;
  } catch (e) { $("#cvInfo").textContent = e.message; }
}
$("#cvClose").onclick = () => $("#convDlg").close();
$("#cvList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cv]"); if (!b) return;
  const cid = b.closest("[data-cid]").dataset.cid, a = b.dataset.cv, { name, host } = cv;
  const w = findWin(name, host);
  if (a === "delete" && !confirm("Delete this conversation's transcript? This cannot be undone.")) return;
  if (a !== "delete" && w?.running && !confirm("claude in this session restarts in that conversation. Continue?")) return;
  b.disabled = true;
  try {
    applySnapshot(await api(`/api/sessions/${encodeURIComponent(name)}/conversations/${cid}`, { method: "POST", body: { action: a }, host }));
    if (a === "delete") { toast("Deleted"); openConversations(name, host); return; }
    $("#convDlg").close(); toast(a === "fork" ? "Forked — new conversation" : "Resumed");
    go(termHash(host, name));
  } catch (err) { toast(err.message); b.disabled = false; }
});

// "Shell": a login shell in tmux on that host (not saved). Listed with the sessions, so you can come back to it.
async function openShell(host = S.hostId) {
  try {
    const r = await api("/api/shells", { method: "POST", body: {}, host });
    applySnapshot(r);
    go(termHash(host, r.name));
  } catch (e) { toast(e.message); }
}

async function hideSession(name, hidden = true, host = S.hostId) {
  try {
    applySnapshot(await sessApi(name, "/hide", { method: "POST", body: { hidden }, host }));
    toast(hidden ? "Removed from view — still running on the machine" : "Brought back");
    return true;
  } catch (e) { toast(e.message); return false; }
}

/* "Close…": ask whether to really end it or only remove it from the view */
let clName = null, clHost = null;
function closeAsk(name, host = S.hostId) {
  const w = findWin(name, host); if (!w) return;
  if (w.shell) { // nothing to keep: a shell is not saved
    if (confirm(`Close ${w.display}? The shell and anything running in it end.`)) deleteSession(name, true, host);
    return;
  }
  clName = name; clHost = host;
  $("#clTitle").textContent = `Close ${w.display}`;
  $("#clInfo").textContent = w.running
    ? `${w.external ? "tmux window opened outside the console" : "Running session"} · ${hostLabel(host)} · ${shortPath(w.cwd)}`
    : `Stopped session · ${hostLabel(host)}`;
  const stop = $('#closeDlg [data-cl="stop"]'), del = $('#closeDlg [data-cl="delete"]'), hide = $('#closeDlg [data-cl="hide"]');
  stop.classList.toggle("hidden", !w.running);
  del.classList.toggle("hidden", !!w.external);
  hide.classList.toggle("hidden", !!w.hidden);
  $("#clStopT").textContent = w.external ? "Close window" : "Stop";
  $("#clStopD").textContent = w.external
    ? "The tmux window and the program in it are terminated (kill-window)."
    : 'claude and its tmux window close; the session card stays. "Resume" continues the same conversation where it left off.';
  $("#closeDlg").showModal();
}
$("#closeDlg").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cl]"); if (!b) return;
  const a = b.dataset.cl, name = clName, host = clHost;
  $("#closeDlg").close();
  if (a === "cancel" || !name) return;
  let ok = false;
  if (a === "hide") ok = await hideSession(name, true, host);
  if (a === "stop") ok = await stopSession(name, true, host);
  if (a === "delete") ok = await deleteSession(name, true, host);
  if (ok) closeEditor();
  const here = S.current === name && S.route === "term" && host === S.hostId;
  if (ok && here) go("#/sessions");
});

/* manage dialog (inside a terminal) */
function openManage() {
  const w = cur(); if (!w || S.route !== "term") return;
  $("#mgTitle").textContent = `${w.display} (${w.name})`;
  $("#mgSend").value = "";
  $$("#manageDlg [data-mg]").forEach((b) => {
    const needsRun = ["interrupt", "send", "text", "copy", "paste", "attach"].includes(b.dataset.mg);
    b.disabled = (needsRun && !w.running) || ((w.external || w.shell) && ["edit", "restart", "resume", "convs"].includes(b.dataset.mg))
      || (b.dataset.mg === "convs" && !w.saved);
  });
  $("#manageDlg").showModal();
}
async function winAction(body) { return api(`/api/windows/${encodeURIComponent(cur().id)}/action`, { method: "POST", body }); }
$("#manageBtn").onclick = openManage;
$("#manageDlg").addEventListener("click", async (e) => {
  const a = e.target.dataset?.mg; if (!a) return;
  const dlg = $("#manageDlg"), w = cur();
  try {
    if (a === "close") dlg.close();
    if (a === "interrupt") { await winAction({ action: "interrupt" }); toast("Ctrl‑C sent"); }
    if (a === "edit") { dlg.close(); openEditor(w.name, S.hostId); }
    if (a === "restart" || a === "resume") {
      if (w.running && !confirm(a === "restart" ? "Restart claude with a new conversation in the same folder?" : "Restart claude in this session's conversation?")) return;
      dlg.close(); await restartSession(w.name, a === "resume");
    }
    if (a === "convs") { dlg.close(); openConversations(w.name); }
    if (a === "copy") { dlg.close(); term.hasSelection() ? copyText(term.getSelection()) : openScreenText(); }
    if (a === "paste") { dlg.close(); pasteFromClipboard(); }
    if (a === "attach") { dlg.close(); pickFiles(); }
    if (a === "fontminus") setFont(fontSize - 1);
    if (a === "fontplus") setFont(fontSize + 1);
    if (a === "text") { dlg.close(); openScreenText(); }
    if (a === "closeask") { dlg.close(); closeAsk(w.name); }
    if (a === "send" && $("#mgSend").value) {
      await winAction({ action: "send", text: $("#mgSend").value, enter: true }); $("#mgSend").value = ""; toast("Sent");
    }
  } catch (err) { toast(err.message); }
});
$("#mgSend").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $('[data-mg="send"]').click(); } });

/* ------------------------------------------------------------------ session editor (create / update / delete) */
let edName = null;           // null = creating
let edHost = "local";        // host the session lives on (chosen in the editor when creating)
let edDirs = null;           // folder suggestions of edHost
let edSettings = null;       // settings of edHost (default command)

async function ensureSettings() { if (!S.settings) { try { S.settings = await api("/api/settings"); } catch {} } return S.settings; }

function renderHostSelect() {
  const list = S.hosts.length ? S.hosts : [{ id: "local", label: S.host, remote: false }];
  $("#fHost").innerHTML = list.map((h) =>
    `<option value="${esc(h.id)}">${h.remote ? "⇄" : "⌂"} ${esc(h.label)}${h.error ? " — not connected" : ""}</option>`).join("");
  $("#fHost").value = edHost;
}
function nextFreeName(hid) {
  const used = new Set(hostWindows(hid).map((x) => x.name));
  let n = 1; while (used.has(`claude-${n}`)) n++;
  return `claude-${n}`;
}
// folder suggestions + default command come from the chosen host
async function loadEditorHost(fillDefaults) {
  const hid = edHost;
  $("#dirPick").innerHTML = `<div class="muted small">Loading folders on ${esc(hostLabel(hid))}…</div>`;
  const [st, d] = await Promise.all([
    api("/api/settings", { host: hid }).catch(() => null),
    api("/api/dirs", { host: hid }).catch(() => ({ recent: [], dirs: [], root: "" })),
  ]);
  if (hid !== edHost) return; // host changed again meanwhile
  edSettings = st; edDirs = d;
  if (fillDefaults) {
    $("#fCmd").value = st?.new_window_command ?? "claude";
    $("#fCwd").value = d.recent[0] ? shortPath(d.recent[0]) : (d.root || "");
  }
  renderDirPick();
}

async function openEditor(name = null, host = S.hostId) {
  edName = name; edHost = host; edDirs = null;
  const w = name ? findWin(name, host) : null;
  $("#edTitle").textContent = w ? (w.saved ? "Edit session" : "Save session") : "New session";
  renderHostSelect();
  $("#fHost").disabled = !!w; // an existing session cannot move to another machine
  if (w) {
    $("#fDisplay").value = w.display === w.name ? "" : w.display;
    $("#fName").value = w.name; $("#fProvider").value = w.provider || "";
    $("#fCwd").value = shortPath(w.cwd || ""); $("#fCmd").value = w.command ?? "";
    $("#fAutostart").checked = !!w.autostart;
  } else {
    $("#fDisplay").value = ""; $("#fName").value = nextFreeName(host); $("#fProvider").value = "";
    $("#fCmd").value = "claude"; $("#fAutostart").checked = false; $("#fCwd").value = "";
  }
  $("#edDelete").classList.toggle("hidden", !w);
  $("#edSaveStart").textContent = !w ? "Create and start" : w.running ? "Save" : "Save and start";
  $("#edSave").classList.toggle("hidden", !!w?.running);
  $("#edSave").textContent = w ? "Save" : "Save only";
  $("#app").classList.add("editor-open"); $("#editor").setAttribute("aria-hidden", "false");
  if (!isTouch()) (w ? $("#fDisplay") : $("#fCwd")).focus();
  await loadEditorHost(!w);
}
function closeEditor() { $("#app").classList.remove("editor-open"); $("#editor").setAttribute("aria-hidden", "true"); }
$("#edClose").onclick = closeEditor;
$("#fHost").addEventListener("change", () => {
  const auto = /^claude-\d+$/.test($("#fName").value);
  edHost = $("#fHost").value;
  if (auto) $("#fName").value = nextFreeName(edHost);
  loadEditorHost(true);
});

function renderDirPick() {
  const d = edDirs || { recent: [], dirs: [] };
  const q = $("#fCwd").value.trim().toLowerCase();
  const recent = d.recent.map((p) => ({ p, r: true }));
  const pool = [...recent, ...d.dirs.filter((p) => !d.recent.includes(p)).map((p) => ({ p }))];
  const exact = (p) => shortPath(p).toLowerCase() === q || p.toLowerCase() === q;
  const list = (q ? pool.filter(({ p }) => p.toLowerCase().includes(q.replace(/^~/, "")) && !exact(p)) : pool).slice(0, 8);
  $("#dirPick").innerHTML = list.map(({ p, r }) => {
    const sp = shortPath(p), cut = sp.lastIndexOf("/");
    return `<button type="button" data-dir="${esc(p)}"><span class="ico">${r ? "↺" : "▸"}</span><span class="dp-parent">${esc(sp.slice(0, cut + 1))}</span><b>${esc(sp.slice(cut + 1))}</b></button>`;
  }).join("") || (q ? `<div class="muted small">No matching folder — you can type a full path.</div>` : "");
}
$("#fCwd").addEventListener("input", renderDirPick);
$("#dirPick").addEventListener("click", (e) => {
  const b = e.target.closest("[data-dir]"); if (!b) return;
  $("#fCwd").value = shortPath(b.dataset.dir); renderDirPick();
  const name = b.dataset.dir.split("/").pop();
  if (!edName && !$("#fDisplay").value) $("#fDisplay").value = name; // a sensible default label
});
$("#cmdChips").addEventListener("click", (e) => { const b = e.target.closest("[data-cmd]"); if (b) $("#fCmd").value = b.dataset.cmd; });

$("#sessForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const start = e.submitter?.value === "start";
  const body = {
    display: $("#fDisplay").value.trim(), provider: $("#fProvider").value.trim(),
    cwd: $("#fCwd").value.trim(), command: $("#fCmd").value.trim(), autostart: $("#fAutostart").checked,
  };
  const name = $("#fName").value.trim(), host = edHost;
  try {
    if (!edName) {
      const snap = await api("/api/sessions", { method: "POST", body: { ...body, name, start }, host });
      applySnapshot(snap); closeEditor();
      toast(start ? `Session opened on ${hostLabel(host)} — claude started` : `Saved on ${hostLabel(host)}`);
      if (start) openSession(name, host);
    } else {
      const was = findWin(edName, host);
      const snap = await sessApi(edName, "", { method: "PATCH", body: { ...body, new_name: name }, host });
      if (S.current === edName && host === S.hostId) { S.current = name; if (S.route === "term") history.replaceState(null, "", termHash(host, name)); }
      applySnapshot(snap);
      closeEditor(); toast("Saved");
      if (start && was && !was.running) await startSession(name, true, host);
    }
  } catch (err) { toast(err.message); }
});
$("#edDelete").onclick = () => closeAsk(edName, edHost);

/* ------------------------------------------------------------------ hosts (local + remote machines) */
function switchHost(hid) {
  if (hid === S.hostId) return;
  closeWs();
  S.hostId = hid; S.windows = S.byHost[hid]?.windows || []; S.current = null; S.settings = null; S.error = null;
  const h = curHost(); S.host = h?.label || hid;
  term.reset();
  startEvents();
  api("/api/windows").then(applySnapshot).catch((e) => toast(e.message));
}

function hostDot(h) {
  if (h.error) return "h-err";
  return h.last_ok ? "h-ok" : "h-wait";
}
function hostSessions(h) {
  // the selected host has fresher data (its own snapshot stream); others come from the host summaries
  if (h.id === S.hostId) return S.windows.map((w) => ({ name: w.name, display: w.display, status: w.status, running: w.running, cwd: w.cwd, hidden: w.hidden, shell: w.shell }));
  return h.sessions || [];
}
function renderHosts() {
  renderHostCards();
  const list = S.hosts.length ? S.hosts : [{ id: "local", label: S.host, remote: false }];
  setHTML($("#hostList"), list.map((h) => {
    const all = hostSessions(h);
    const sess = all.filter((x) => !x.hidden), nHidden = all.length - sess.length;
    const busy = all.filter((x) => x.status === "working").length;
    return `<li class="host-row ${h.id === S.hostId ? "cur" : ""}" data-host="${esc(h.id)}" title="${esc(h.error || `${h.username || ""}@${h.hostname || ""}`)}">
      <span class="hico">${h.remote ? "⇄" : "⌂"}</span><span class="nm">${esc(h.label)}</span>
      <span class="dot ${hostDot(h)}" title="${h.error ? "not connected" : "connected"}"></span><span class="tag" title="working / total">${busy ? `<b class="busy">${busy}</b>/` : ""}${sess.length}</span>
      ${h.remote ? `<button class="icon-btn edit" data-hedit="${esc(h.id)}" title="Edit host">✎</button>` : ""}
    </li>` + (sess.length ? sess.map((x) => `
    <li class="sess s-${x.status} ${x.running ? "" : "off"} ${S.route === "term" && h.id === S.hostId && x.name === S.current ? "active" : ""}"
        data-host="${esc(h.id)}" data-name="${esc(x.name)}" title="${esc(STATUS_LABEL[x.status] || x.status)}${x.cwd ? " · " + esc(shortPath(x.cwd)) : ""}">
      <span class="dot"></span>${x.shell ? '<span class="sh">$</span>' : ""}<span class="nm">${esc(x.display)}</span>
      <button class="icon-btn tclose" data-tclose title="Close / hide…">✕</button>
    </li>`).join("") : `<li class="empty-sess">${h.error ? "cannot connect" : "no sessions"}</li>`)
      + (nHidden ? `<li class="more" data-host="${esc(h.id)}" data-hidden="1">+${nHidden} hidden (running)</li>` : "");
  }).join(""));
}
$("#hostList").addEventListener("click", async (e) => {
  const ed = e.target.closest("[data-hedit]");
  if (ed) { e.stopPropagation(); return openHostEditor(ed.dataset.hedit); }
  const x = e.target.closest("[data-tclose]");
  if (x) { // ✕ on a tree item: the same "close or only hide?" dialog as on the cards
    e.stopPropagation();
    const li = x.closest("[data-name]"), host = li.dataset.host, name = li.dataset.name;
    if (!findWin(name, host)) { // another host's full list may not be loaded yet
      try { applySnapshot(await api("/api/windows", { host })); } catch (err) { return toast(err.message); }
    }
    return closeAsk(name, host);
  }
  const li = e.target.closest("[data-host]"); if (!li) return;
  const h = encodeURIComponent(li.dataset.host);
  if (li.dataset.hidden) { setFilter("hidden"); return go(`#/h/${h}/sessions`); }
  go(li.dataset.name ? `#/h/${h}/term/${encodeURIComponent(li.dataset.name)}` : `#/h/${h}/sessions`);
});
$("#newHostBtn").onclick = () => openHostEditor();
$("#addHostBtn").onclick = () => openHostEditor();

/* hosts page: list + CRUD for machines (the side panel #hostEditor does create/update) */
function renderHostCards() {
  $("#hostCount").textContent = S.hosts.length || 1;
  if (S.route !== "hosts") return;
  const list = S.hosts.length ? S.hosts : [{ id: "local", label: S.host, remote: false }];
  setHTML($("#hostCards"), list.map((h) => {
    const all = hostSessions(h), i = h.info || {};
    const busy = all.filter((x) => x.status === "working").length, running = all.filter((x) => x.running).length;
    const addr = h.remote ? `${h.username}@${h.hostname}${h.port && h.port !== 22 ? ":" + h.port : ""}` : `${h.username || S.user || ""}@${h.hostname || "localhost"}`;
    const AUTH = { "custom-key": "pasted private key", "password": "password (Vault)", "console-key": "console key", "local": "local" };
    const miss = h.remote ? [!i.tmux && "tmux", !i.claude && "claude"].filter(Boolean) : [];
    return `<div class="card host-card" data-hid="${esc(h.id)}">
      <div class="card-head">
        <span class="tile">${h.remote ? "⇄" : "⌂"}<span class="dot ${hostDot(h)}" title="${h.error ? "not connected" : "connected"}"></span></span>
        <div class="card-title"><b>${esc(h.label)}</b><span class="muted small"><code>${esc(addr)}</code></span></div>
        ${h.remote ? `<button class="icon-btn" data-ha="edit" title="Edit host">✎</button>` : ""}
      </div>
      ${h.error ? `<div class="card-path danger-text">${esc(h.error)}</div>` : ""}
      <div class="card-path">${[i.os, i.tmux, i.claude && "claude " + i.claude.split(" ")[0]].filter(Boolean).map(esc).join(" · ") || (h.remote ? "not probed yet" : "this machine")}
        ${miss.length ? ` · <span class="danger-text">missing: ${miss.join(", ")}</span>` : ""}</div>
      <div class="card-foot">
        <span><span class="lbl">${all.length}</span> session${all.length === 1 ? "" : "s"} · <span class="lbl">${running}</span> running${busy ? ` · <b class="busy">${busy}</b> working` : ""}</span>
        ${h.remote ? `<span class="pill">${esc(AUTH[h.auth] || h.auth || "")}</span>` : ""}
        <span class="grow"></span>
        ${h.remote ? `<button class="btn danger" data-ha="delete">Delete</button><button class="btn" data-ha="test">Test</button>` : ""}
        <button class="btn" data-ha="shell" title="Login shell on this host in tmux — not saved; come back to it any time, exit closes it">Shell</button>
        <button class="btn primary" data-ha="sessions">Sessions</button>
      </div>
    </div>`;
  }).join(""));
}
$("#hostCards").addEventListener("click", async (e) => {
  const card = e.target.closest("[data-hid]"); if (!card) return;
  const id = card.dataset.hid, a = e.target.closest("[data-ha]")?.dataset.ha || "sessions";
  const enc = encodeURIComponent(id);
  if (a === "edit") return openHostEditor(id);
  if (a === "shell") return openShell(id);
  if (a === "sessions") return go(`#/h/${enc}/sessions`);
  if (a === "test") {
    const b = e.target.closest("button"); b.disabled = true; b.textContent = "Testing…";
    try {
      const info = await api(`/api/hosts/${enc}/test`, { method: "POST" });
      toast(info.ok ? "Connection OK" : "Connected, but the command failed");
    } catch (err) { toast(`Connection failed: ${err.message}`); }
    try { S.hosts = (await api("/api/hosts")).hosts; } catch {}
    b.disabled = false; b.textContent = "Test"; renderHosts(); renderHostCards();
  }
  if (a === "delete") {
    const h = S.hosts.find((x) => x.id === id);
    if (!confirm(`Remove "${h?.label}" from the list?\ntmux sessions on the remote host are not touched; saved session definitions also stay in this console.`)) return;
    try {
      S.hosts = (await api(`/api/hosts/${enc}`, { method: "DELETE" })).hosts; toast("Removed");
      if (S.hostId === id) switchHost("local");
      renderHosts(); renderHostCards();
    } catch (err) { toast(err.message); }
  }
});

let hEditing = null, hAuth = "password";
function setAuthPane(a) {
  hAuth = a;
  $$("#hAuth button").forEach((b) => b.classList.toggle("on", b.dataset.a === a));
  $$("#hostForm [data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== a));
}
$("#hAuth").addEventListener("click", (e) => { const b = e.target.closest("[data-a]"); if (b) setAuthPane(b.dataset.a); });

async function openHostEditor(hid = null) {
  hEditing = hid;
  const h = hid ? S.hosts.find((x) => x.id === hid) : null;
  $("#hTitle").textContent = h ? "Edit host" : "Add remote host";
  $("#hLabel").value = h?.label || ""; $("#hHostname").value = h?.hostname || "";
  $("#hPort").value = h?.port || 22; $("#hUser").value = h?.username || "";
  $("#hPassword").value = ""; $("#hKey").value = ""; $("#hStorePw").checked = h?.auth === "password";
  setAuthPane(h ? "manual" : "password");
  const AUTH_LABEL = { "custom-key": "pasted private key", "password": "password stored in Vault", "console-key": "console key" };
  $("#hAuthNow").textContent = h ? `Currently: ${AUTH_LABEL[h.auth] || h.auth}. Enter a password or a new key to change it.` : "";
  $("#hDelete").classList.toggle("hidden", !h);
  $("#hTest").classList.toggle("hidden", !h);
  $("#hShell").classList.toggle("hidden", !h);
  $("#hSave").textContent = h ? "Save" : "Save and connect";
  renderHostInfo(h);
  $("#app").classList.add("host-open"); $("#hostEditor").setAttribute("aria-hidden", "false");
  try { $("#hPub").textContent = (await api("/api/ssh-key")).public_key; } catch {}
  if (!isTouch()) (h ? $("#hLabel") : $("#hHostname")).focus();
}
function closeHostEditor() { $("#app").classList.remove("host-open"); $("#hostEditor").setAttribute("aria-hidden", "true"); }
$("#hClose").onclick = closeHostEditor;
$("#hCopyPub").onclick = () => copyText($("#hPub").textContent);

function renderHostInfo(h, info = h?.info) {
  $("#hInfoBox").classList.toggle("hidden", !info && !h?.error);
  const row = (k, v, bad) => `<dt>${k}</dt><dd class="${bad ? "bad" : ""}">${esc(v)}</dd>`;
  $("#hInfo").innerHTML = (h?.error ? row("Status", h.error, true) : "") + (info ? [
    row("Hostname", info.host || "?"), row("System", info.os || "?"),
    row("tmux", info.tmux || "not installed", !info.tmux), row("claude", info.claude || "not installed / not in PATH", !info.claude),
  ].join("") : "");
}

$("#hostForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { label: $("#hLabel").value.trim(), hostname: $("#hHostname").value.trim(),
                 port: +$("#hPort").value || 22, username: $("#hUser").value.trim() };
  if (hAuth === "password" && $("#hPassword").value) { body.password = $("#hPassword").value; body.store_password = $("#hStorePw").checked; }
  if (hAuth === "manual") body.auth = "manual";
  if (hAuth === "key" && $("#hKey").value.trim()) body.private_key = $("#hKey").value.trim();
  if (!hEditing && hAuth === "password" && !body.password) return toast("Enter a password (or choose Manual / Private key)");
  const btn = $("#hSave"); btn.disabled = true; btn.textContent = "Connecting…";
  try {
    if (hEditing) {
      S.hosts = (await api(`/api/hosts/${encodeURIComponent(hEditing)}`, { method: "PATCH", body })).hosts;
      toast("Saved"); closeHostEditor();
      if (hEditing === S.hostId) { const id = S.hostId; S.hostId = ""; switchHost(id); }
    } else {
      const r = await api("/api/hosts", { method: "POST", body });
      S.hosts = r.hosts; closeHostEditor();
      const i = r.host.info || {};
      toast(`Connected: ${r.host.label}` + (!i.tmux || !i.claude ? " — something is not installed" : ""));
      go(`#/h/${encodeURIComponent(r.host.id)}/sessions`);
    }
    renderHosts();
  } catch (err) { toast(err.message); }
  btn.disabled = false; btn.textContent = hEditing ? "Save" : "Save and connect";
});
$("#hTest").onclick = async () => {
  const b = $("#hTest"); b.disabled = true; b.textContent = "Testing…";
  try {
    const info = await api(`/api/hosts/${encodeURIComponent(hEditing)}/test`, { method: "POST" });
    renderHostInfo(null, info); toast(info.ok ? "Connection OK" : "Connected, but the command failed");
    S.hosts = (await api("/api/hosts")).hosts; renderHosts();
  } catch (err) { renderHostInfo({ error: err.message }); toast("Connection failed"); }
  b.disabled = false; b.textContent = "Test connection";
};
$("#hShell").onclick = () => { const id = hEditing; closeHostEditor(); openShell(id); };
$("#hDelete").onclick = async () => {
  const h = S.hosts.find((x) => x.id === hEditing);
  if (!confirm(`Remove "${h?.label}" from the list?\ntmux sessions on the remote host are not touched; saved session definitions also stay in this console.`)) return;
  try {
    S.hosts = (await api(`/api/hosts/${encodeURIComponent(hEditing)}`, { method: "DELETE" })).hosts;
    closeHostEditor(); toast("Removed");
    if (S.hostId === hEditing) go("#/h/local/sessions"); else renderHosts();
  } catch (err) { toast(err.message); }
};

/* ------------------------------------------------------------------ routing */
function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }

function route() {
  let parts = location.hash.replace(/^#\/?/, "").split("/");
  const hostScoped = parts[0] === "h";
  if (hostScoped) {
    const hid = decodeURIComponent(parts[1] || "local");
    if (hid !== S.hostId) switchHost(hid);
    parts = parts.slice(2);
  }
  const [page, arg] = parts;
  const focusHost = hostScoped && page === "sessions" ? S.hostId : null;
  const r = ["sessions", "hosts", "team", "settings", "term", "shell"].includes(page) ? page : "sessions";
  const wantKind = r === "shell" ? "shell" : r === "term" ? "tmux" : null;
  if (ws && wantKind !== wsKind) closeWs();
  if (r === "term") {
    const name = decodeURIComponent(arg || "");
    if (S.windows.length && !S.windows.find((w) => w.name === name)) { go("#/sessions"); return; }
    if (!S.windows.length) { S.current = name; S.route = "term"; }
    const switching = S.current !== name;
    S.current = name;
    const w = cur();
    if (switching && w?.running && ws && ws.readyState === 1 && w.tmux_session === wsSess) { wsSend({ t: "select", w: w.id }); if (!isTouch()) term.focus(); }
    else if (switching && ws) closeWs();
  }
  S.route = r;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${r === "shell" ? "term" : r}`));
  $$("nav [data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === r));
  $("#app").classList.remove("menu-open");
  if (isTermRoute()) requestAnimationFrame(() => { fit.fit(); if (!ws) connect(); });
  else closeWs();
  if (r === "team") { S.unread = 0; renderUnread(); renderBoard(true); }
  if (r === "settings") { loadSettings(); loadLogs(); }
  if (r === "sessions") loadOtherHosts();
  renderAll();
  if (focusHost) requestAnimationFrame(() => document.getElementById(`grp-${focusHost}`)?.scrollIntoView({ block: "start" }));
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ rendering */
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}
// replace innerHTML only when it really changed: periodic refreshes must not eat taps/clicks
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
const shortPath = (p) => (p || "").replace(/^\/home\/[^/]+(?=\/|$)/, "~").replace(/^\/root(?=\/|$)/, "~");
const initials = (s) => (s || "?").replace(/\(.*?\)/g, "").trim().split(/[\s._-]+/).map((x) => x[0]).join("").slice(0, 2).toUpperCase() || "?";

function renderAll() {
  if (document.hidden) { renderAll.pending = true; return; }
  renderSidebar(); renderHead(); renderCards(); renderTargets(); renderHostCards();
}

function renderSidebar() {
  const running = S.windows.filter((w) => w.running);
  $("#sessionCount").textContent = (S.hosts.length ? S.hosts : [{ id: S.hostId }])
    .reduce((n, h) => n + hostSessions(h).filter((x) => !x.hidden).length, 0);
  $("#whoUser").textContent = S.user || "";
  const nh = S.hosts.length || 1;
  $("#whoHost").textContent = `${nh} host${nh === 1 ? "" : "s"} · ${running.length} running here`;
  renderHosts();
  $("#avatar").textContent = (S.user || "?")[0].toUpperCase();
}

function renderHead() {
  updateSizeBar();
  $("#manageBtn").classList.toggle("hidden", S.route === "shell");
  if (S.route === "shell") {
    $("#tDisplay").textContent = `Shell · ${S.host}`; $("#tName").textContent = `${S.login}@${S.host}`;
    $("#tStatus").className = "status s-shell"; $("#tStatus").innerHTML = `<span class="dot"></span>login shell (outside tmux)`;
    $("#tProvider").textContent = ""; $("#tPath").textContent = "";
    document.title = `Shell · ${S.host} · devbox`;
    return;
  }
  const w = cur(); if (!w) return;
  $("#tDisplay").textContent = w.display;
  $("#tName").textContent = w.name;
  $("#tStatus").className = `status s-${w.status}`;
  $("#tStatus").innerHTML = `<span class="dot"></span>${esc(STATUS_LABEL[w.status] || w.status)}`;
  $("#tProvider").textContent = w.provider || "";
  $("#tPath").textContent = w.title ? `${shortPath(w.cwd)}: ${w.title}` : shortPath(w.cwd);
  if (S.route === "term") document.title = `${w.display} · devbox`;
}

/* Sessions page: every host's sessions, grouped by host. The selected host is live (SSE);
   the others are refreshed by loadOtherHosts() while this page is open. */
let othersBusy = false;
async function loadOtherHosts(refresh = false) {
  if (othersBusy) return;
  othersBusy = true;
  try {
    await Promise.all(S.hosts.filter((h) => h.id !== S.hostId).map((h) =>
      api(`/api/windows${refresh ? "?refresh=1" : ""}`, { host: h.id })
        .then((snap) => { S.byHost[h.id] = snap; })
        .catch((e) => { S.byHost[h.id] = { host_id: h.id, windows: S.byHost[h.id]?.windows || [], error: e.message }; })));
  } finally { othersBusy = false; }
  renderCards(); renderSidebar();
}

function cardHTML(w, hid) {
  return `
    <div class="card s-${w.status} ${w.running ? "" : "off"} ${w.hidden ? "hidden-card" : ""}" data-host="${esc(hid)}" data-name="${esc(w.name)}">
      <div class="card-head">
        <span class="tile ${w.shell ? "tile-shell" : ""}">${w.shell ? "$" : esc(initials(w.display))}<span class="dot" title="${esc(STATUS_LABEL[w.status] || w.status)}"></span></span>
        <div class="card-title"><b>${esc(w.display)}</b><span class="muted small"><code>${esc(w.name)}</code>${w.shell ? " · shell, not saved" : w.saved || w.external ? "" : " · unsaved"}</span></div>
        ${w.saved && !w.shell ? `<button class="icon-btn" data-act="convs" title="Claude conversations">${ICON_HISTORY}</button>` : ""}
        ${w.external || w.shell ? "" : `<button class="icon-btn" data-act="edit" title="Edit">✎</button>`}
        <button class="icon-btn" data-act="close" title="Close / hide">✕</button>
      </div>
      <div class="card-path" title="${esc(w.cwd)}">▭ ${esc(shortPath(w.cwd) || "—")}</div>
      ${w.running ? `<pre>${esc((w.preview || []).join("\n"))}</pre>` : ""}
      <div class="card-foot">
        ${w.external ? `<span class="pill ext" title="tmux session opened outside the console: ${esc(w.tmux_session)}">external · ${esc(w.tmux_session)}</span>` : ""}
        ${w.hidden ? `<button class="btn" data-act="unhide">Bring back</button>` : ""}
        ${w.provider ? `<span class="pill">✦ ${esc(w.provider)}</span>` : ""}
        <code class="cmd">${esc(w.command || w.cmd || "")}</code>
        ${w.autostart ? '<span class="pill" title="Starts automatically">auto</span>' : ""}
        <span class="grow"></span>
        <span class="muted">${ago(w.last_change)}</span>
        ${w.running
          ? (w.status === "dead" ? `<button class="btn" data-act="restart">Restart</button>` : "") + `<button class="btn primary" data-act="open">Connect</button>`
          : `<button class="btn" data-act="fresh" title="New conversation">Start</button><button class="btn primary" data-act="resume" title="claude --continue">Resume</button>`}
      </div>
    </div>`;
}

function groupHead(h, wins) {
  const i = h.info || {};
  const miss = h.remote ? [!i.tmux && "tmux", !i.claude && "claude"].filter(Boolean) : [];
  const err = h.id === S.hostId ? S.error || h.error : S.byHost[h.id]?.error || h.error;
  const addr = h.remote ? `${h.username}@${h.hostname}${h.port && h.port !== 22 ? ":" + h.port : ""}` : "this machine";
  const running = wins.filter((w) => w.running).length;
  return `<div class="grp-head ${err || miss.length ? "warn" : ""}" data-host="${esc(h.id)}">
      <span class="hico">${h.remote ? "⇄" : "⌂"}</span><b>${esc(h.label)}</b>
      <span class="dot ${hostDot(h)}" title="${err ? "not connected" : "connected"}"></span>
      <code class="muted small hide-mobile">${esc(addr)}</code>
      <span class="muted small">${wins.length} · ${running} running</span>
      ${err ? `<span class="danger-text small">${esc(err)}</span>` : ""}
      ${miss.length ? `<span class="small">⚠ Missing: <b>${miss.join(", ")}</b></span>` : ""}
      <span class="grow"></span>
      <button class="btn" data-ga="shell" title="Login shell on this host in tmux — not saved; come back to it any time, exit closes it">Shell</button>
      <button class="btn" data-ga="new">＋ New<span class="hide-mobile"> session</span></button>
    </div>`;
}

function renderCards() {
  $("#errorBox").classList.add("hidden");
  const q = $("#sessFilter").value.trim().toLowerCase();
  const hosts = S.hosts.length ? S.hosts : [{ id: S.hostId, label: S.host, remote: S.hostId !== "local" }];
  const every = hosts.flatMap((h) => hostWindows(h.id));
  const nHidden = every.filter((w) => w.hidden).length;
  $("#hiddenSeg").textContent = nHidden ? `Hidden (${nHidden})` : "Hidden";
  $("#startAllBtn").classList.toggle("hidden", !every.some((w) => !w.running && !w.hidden));
  const match = (w) =>
    (S.filter === "hidden" ? w.hidden : !w.hidden && (S.filter === "all" || (S.filter === "running") === !!w.running)) &&
    (!q || [w.display, w.name, w.cwd, w.provider, w.command].join(" ").toLowerCase().includes(q));
  const narrowed = q || S.filter !== "all";
  let shown = 0;
  const groups = hosts.map((h) => {
    const wins = hostWindows(h.id), list = wins.filter(match);
    shown += list.length;
    if (narrowed && !list.length) return "";
    const body = list.length ? `<div class="cards">${list.map((w) => cardHTML(w, h.id)).join("")}</div>`
      : `<div class="grp-empty muted small">${S.byHost[h.id] || h.id === S.hostId ? "No sessions on this host." : "Loading…"}
          <button class="btn" data-ga="new">＋ New session here</button></div>`;
    return `<section class="grp" id="grp-${esc(h.id)}" data-host="${esc(h.id)}">${groupHead(h, wins.filter((w) => !w.hidden))}${body}</section>`;
  }).join("");
  setHTML($("#cards"), narrowed && !shown ? `<div class="empty">No matching session.</div>` : groups);
}

function renderTargets() {
  const tgt = (w) => (S.hostId === "local" ? w.name : `${w.name}@${S.hostId}`);
  const opts = S.windows.map((w) => `<option value="${esc(tgt(w))}">${esc(w.display)} (${esc(tgt(w))})</option>`).join("");
  const sets = [[$("#msgTo"), `<option value="all">Herkes</option>${opts}`],
                [$("#teamFilter"), `<option value="">All</option><option value="user">${esc(S.user || "User")}</option>${opts}`]];
  for (const [sel, html] of sets) {
    if (sel.dataset.html === html) continue;
    const v = sel.value; sel.innerHTML = html; sel.dataset.html = html;
    sel.value = [...sel.options].some((o) => o.value === v) ? v : sel.options[0].value;
  }
}

$("#cards").addEventListener("click", async (e) => {
  const ga = e.target.closest("[data-ga]");
  if (ga) {
    const hid = ga.closest("[data-host]").dataset.host;
    if (ga.dataset.ga === "new") return openEditor(null, hid);
    if (ga.dataset.ga === "shell") return openShell(hid);
    return;
  }
  const card = e.target.closest(".card"); if (!card) return;
  const act = e.target.closest("[data-act]")?.dataset.act;
  const name = card.dataset.name, host = card.dataset.host, w = findWin(name, host);
  if (act === "edit") return openEditor(name, host);
  if (act === "close") return closeAsk(name, host);
  if (act === "unhide") return hideSession(name, false, host);
  if (act === "resume") return startSession(name, true, host);
  if (act === "fresh") return startSession(name, false, host);
  if (act === "restart") {
    if (w.saved) return restartSession(name, true, host);
    try { await api(`/api/windows/${encodeURIComponent(w.id)}/action`, { method: "POST", body: { action: "respawn", command: w.command || "claude" }, host }); toast("Restarted"); }
    catch (err) { toast(err.message); }
    return;
  }
  if (act === "convs") return openConversations(name, host);
  if (act === "open" || w?.running) return openSession(name, host);
  openEditor(name, host); // a stopped card: show its details
});
$("#sessFilter").addEventListener("input", renderCards);
function setFilter(f) {
  S.filter = f; $$("#sessView button").forEach((x) => x.classList.toggle("on", x.dataset.f === f)); renderCards();
}
$("#sessView").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) setFilter(b.dataset.f); });
$("#refreshBtn").onclick = async () => {
  try { applySnapshot(await api("/api/windows?refresh=1")); } catch (e) { toast(e.message); }
  await loadOtherHosts(true); toast("Refreshed");
};
$("#startAllBtn").onclick = async () => {
  const stopped = (S.hosts.length ? S.hosts : [{ id: S.hostId }]).flatMap((h) =>
    hostWindows(h.id).filter((w) => !w.running && !w.hidden).map((w) => ({ name: w.name, host: h.id })));
  for (const w of stopped) {
    try { applySnapshot(await sessApi(w.name, "/start", { method: "POST", body: { resume: true }, host: w.host })); }
    catch (e) { toast(`${w.name}@${hostLabel(w.host)}: ${e.message}`); }
  }
  toast(`${stopped.length} sessions started`);
};
$("#newSessionBtn").onclick = () => openEditor();
$("#newSessionBtn2").onclick = () => openEditor();

$("nav").addEventListener("click", (e) => { const b = e.target.closest("[data-nav]"); if (b) go(`#/${b.dataset.nav}`); });

/* ------------------------------------------------------------------ team */
function fmtText(t) { return esc(t).replace(/`([^`\n]+)`/g, "<code>$1</code>"); }
function fmtTime(ts) {
  const d = new Date(ts * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}
function nameOf(n) {
  if (n === "user") return S.user || "User";
  const [name, host = "local"] = n.split("@");
  const w = host === S.hostId ? S.windows.find((x) => x.name === name) : null;
  const h = host !== "local" ? S.hosts.find((x) => x.id === host) : null;
  return (w ? w.display : name) + (h ? ` @${h.label}` : "");
}

function renderBoard(scroll) {
  const f = $("#teamFilter").value;
  const list = S.messages.filter((m) => !f || m.from === f || m.to === f || (m.to === "all" && f !== "user"));
  const b = $("#board");
  const atBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 60;
  b.innerHTML = list.length ? list.map((m) => `
    <div class="msg ${m.from === "user" ? "mine" : ""}">
      <div class="msg-head"><b>${esc(nameOf(m.from))}</b><span class="to">→ ${esc(m.to === "all" ? "herkes" : nameOf(m.to))}</span>
        <span>${fmtTime(m.ts)}</span>${m.delivered?.length ? `<span class="dlv" title="delivered with send-keys">✓ ${esc(m.delivered.join(", "))}</span>` : ""}</div>
      <div class="msg-body">${fmtText(m.text)}</div>
    </div>`).join("") : `<div class="empty">No messages yet.<br><span class="small">Sessions post with <code>devbox-msg post "…"</code> and read with <code>devbox-msg read</code>.</span></div>`;
  if (scroll || atBottom) b.scrollTop = b.scrollHeight;
}
function renderUnread() { $("#unread").textContent = S.unread; $("#unread").classList.toggle("hidden", !S.unread); }
function addMessage(m) {
  if (m.id <= S.lastMsgId) return;
  S.messages.push(m); S.lastMsgId = m.id;
  if (S.route === "team") renderBoard(); else if (m.from !== "user") { S.unread++; renderUnread(); }
}
async function loadMessages() {
  const { messages } = await api(`/api/messages?limit=500`);
  S.messages = messages; S.lastMsgId = messages.at(-1)?.id || 0;
  if (S.route === "team") renderBoard(true);
}
$("#teamFilter").onchange = () => renderBoard(true);
$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#msgText").value.trim(); if (!text) return;
  try {
    const m = await api("/api/messages", { method: "POST", body: { from: "user", to: $("#msgTo").value, text, deliver: $("#msgDeliver").checked } });
    $("#msgText").value = ""; addMessage(m); renderBoard(true);
  } catch (err) { toast(err.message); }
});
$("#msgText").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#composer").requestSubmit(); });

/* ------------------------------------------------------------------ settings */
async function loadSettings() {
  const s = S.settings = await api("/api/settings");
  $("#envRows").innerHTML = s.env.map((e) => `<tr><td><code>${esc(e.name)}</code></td><td class="muted"><code>${esc(e.masked)}</code></td>
      <td><button class="btn" data-edit="${esc(e.name)}">Change</button> <button class="btn danger" data-del="${esc(e.name)}">Delete</button></td></tr>`).join("")
    || `<tr><td class="muted">No variables defined.</td></tr>`;
  $("#newCmdDefault").value = s.new_window_command || "";
  $("#projectsRoot").value = s.projects_root || "";
  $("#pubUrl").textContent = s.public_url;
  $("#apiToken").textContent = s.token;
  $("#tmuxInfo").textContent = `${s.user} · ${s.host} · session ${s.session}`;
  $("#rootHost").textContent = s.host;
  $("#pubKey").textContent = s.public_key || "";
  const st = s.secrets || {};
  $("#storeInfo").textContent = st.kind === "vault" ? `HashiCorp Vault · ${st.location} · ${st.mount}/ (${st.mode === "managed" ? "managed by the console" : "external"})` : `file (0600) · ${st.location}`;
  $("#msgInstall").textContent = `mkdir -p ~/.local/bin && curl -fsS ${s.public_url}/devbox-msg -o ~/.local/bin/devbox-msg && chmod +x ~/.local/bin/devbox-msg`;
}
async function loadLogs() {
  try {
    const r = await api("/api/logs?lines=400");
    const el = $("#logText");
    el.textContent = r.lines.length ? r.lines.join("\n") : "No events yet.";
    $("#logInfo").textContent = `Terminal connects/disconnects (who closed it, after how long, from which device), host reachability, sessions, uploads, logins and console restarts. ` +
      `${r.files} day file${r.files === 1 ? "" : "s"} · ${Math.ceil(r.bytes / 1024)} KB · kept ${r.days} days, then deleted automatically.`;
    el.scrollTop = el.scrollHeight;
  } catch (e) { $("#logText").textContent = e.message; }
}
$("#logRefresh").onclick = loadLogs;
$("#logClear").onclick = async () => {
  if (!confirm("Delete the whole event log?")) return;
  try { await api("/api/logs", { method: "DELETE" }); toast("Log cleared"); loadLogs(); } catch (e) { toast(e.message); }
};
$("#saveDefaults").onclick = async () => {
  try {
    await api("/api/settings", { method: "PUT", body: { new_window_command: $("#newCmdDefault").value.trim(), projects_root: $("#projectsRoot").value.trim() } });
    S.settings = null; toast("Saved");
  } catch (err) { toast(err.message); }
};
$("#envForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/env", { method: "PUT", body: { name: $("#envName").value.trim(), value: $("#envValue").value } });
    $("#envName").value = ""; $("#envValue").value = ""; toast("Saved — applies to new/restarted sessions"); loadSettings();
  } catch (err) { toast(err.message); }
});
$("#envRows").addEventListener("click", async (e) => {
  if (e.target.dataset.edit) { $("#envName").value = e.target.dataset.edit; $("#envValue").focus(); }
  if (e.target.dataset.del && confirm(`Delete ${e.target.dataset.del}?`)) {
    try { await api("/api/env", { method: "PUT", body: { name: e.target.dataset.del, delete: true } }); loadSettings(); } catch (err) { toast(err.message); }
  }
});
$("#copyPubKey").onclick = () => copyText($("#pubKey").textContent);
$("#apiToken").onclick = (e) => e.target.classList.toggle("blur");
$("#logoutBtn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.href = "/login"; };

/* ------------------------------------------------------------------ palette (Ctrl K) */
let palItems = [], palSel = 0;
function openPalette() { $("#paletteInput").value = ""; palSel = 0; renderPalette(); $("#palette").showModal(); $("#paletteInput").focus(); }
function renderPalette() {
  const q = $("#paletteInput").value.toLowerCase();
  const all = [
    ...(S.hosts.length ? S.hosts : [{ id: S.hostId }]).flatMap((h) => hostWindows(h.id).map((w) => ({
      label: w.display + (w.hidden ? " (hidden)" : ""), dot: w.status, run: () => openSession(w.name, h.id),
      k: `${w.name}${S.hosts.length > 1 ? " @" + hostLabel(h.id) : ""} · ${STATUS_LABEL[w.status] || ""} · ${shortPath(w.cwd)}` }))),
    ...(S.hosts.length ? S.hosts : [{ id: S.hostId, label: S.host }]).map((h) => ({ label: `New shell on ${h.label}`, k: "command", run: () => openShell(h.id) })),
    ...S.hosts.filter((h) => h.id !== S.hostId).map((h) => ({ label: `Host: ${h.label}`, k: `${h.username}@${h.hostname}`, run: () => go(`#/h/${encodeURIComponent(h.id)}/sessions`) })),
    { label: "Add remote host…", k: "command", run: () => openHostEditor() },
    { label: "New session…", k: "command", run: () => openEditor() },
    { label: "Sessions", k: "page", run: () => go("#/sessions") },
    { label: "Hosts", k: "page", run: () => go("#/hosts") },
    { label: "Team", k: "page", run: () => go("#/team") },
    { label: "Settings", k: "page", run: () => go("#/settings") },
  ];
  palItems = all.filter((i) => (i.label + " " + i.k).toLowerCase().includes(q));
  palSel = Math.min(palSel, Math.max(0, palItems.length - 1));
  $("#paletteList").innerHTML = palItems.map((i, n) => `<li class="${n === palSel ? "sel" : ""} ${i.dot ? "s-" + i.dot : ""}" data-n="${n}">
    ${i.dot ? '<span class="dot"></span>' : '<span class="ico">›</span>'}<span>${esc(i.label)}</span><span class="k">${esc(i.k)}</span></li>`).join("");
}
function runPalette(n) { const i = palItems[n]; $("#palette").close(); if (i) i.run(); }
$("#openPalette").onclick = openPalette;
$("#paletteInput").addEventListener("input", () => { palSel = 0; renderPalette(); });
$("#paletteInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { palSel = Math.min(palSel + 1, palItems.length - 1); renderPalette(); e.preventDefault(); }
  if (e.key === "ArrowUp") { palSel = Math.max(palSel - 1, 0); renderPalette(); e.preventDefault(); }
  if (e.key === "Enter") { runPalette(palSel); e.preventDefault(); }
});
$("#paletteList").addEventListener("click", (e) => { const li = e.target.closest("li"); if (li) runPalette(+li.dataset.n); });
$("#palette").addEventListener("click", (e) => { if (e.target === $("#palette")) $("#palette").close(); });
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); e.stopPropagation(); openPalette(); }
  if (e.key === "Escape" && $("#app").classList.contains("editor-open")) closeEditor();
  if (e.key === "Escape" && $("#app").classList.contains("host-open")) closeHostEditor();
}, true);

/* ------------------------------------------------------------------ mobile drawer */
$$("[data-menu]").forEach((b) => (b.onclick = () => $("#app").classList.add("menu-open")));
$("#closeSidebar").onclick = () => $("#app").classList.remove("menu-open");
$("#scrim").onclick = () => { $("#app").classList.remove("menu-open"); closeEditor(); closeHostEditor(); };
(() => { // swipe from the left edge opens the drawer
  let x0 = null;
  document.addEventListener("touchstart", (e) => { const x = e.touches[0].clientX; x0 = x < 18 ? x : null; }, { passive: true });
  document.addEventListener("touchend", (e) => {
    if (x0 != null && e.changedTouches[0].clientX - x0 > 60) $("#app").classList.add("menu-open"); x0 = null;
  }, { passive: true });
})();

/* ------------------------------------------------------------------ live events */
let es = null;
function startEvents() {
  if (es) es.close();
  es = new EventSource(withHost("/api/events"));
  es.addEventListener("windows", (e) => applySnapshot(JSON.parse(e.data)));
  es.addEventListener("hosts", (e) => { S.hosts = JSON.parse(e.data); renderHosts(); renderHostCards(); if (S.route === "sessions") renderCards(); });
  es.addEventListener("message", (e) => addMessage(JSON.parse(e.data)));
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (renderAll.pending) { renderAll.pending = false; renderAll(); }
  if (isTermRoute() && !ws) connect();
  else { checkAlive(); claimSize(); }
});
window.addEventListener("online", () => { if (isTermRoute()) (ws ? checkAlive() : connect()); });

(async function init() {
  const m = location.hash.match(/^#\/h\/([^/]+)/);
  if (m) S.hostId = decodeURIComponent(m[1]);
  try { S.hosts = (await api("/api/hosts")).hosts; } catch {}
  api("/api/whoami").then((w) => {
    $("#version").textContent = `devbox v${w.version}${w.commit ? " · " + w.commit : ""}`;
    $("#version").title = "Console version · git commit of this build";
  }).catch(() => {});
  if (!S.hosts.some((h) => h.id === S.hostId)) S.hostId = "local";
  try { applySnapshot(await api("/api/windows")); } catch {}
  ensureSettings();
  loadMessages().catch(() => {});
  if (!location.hash) {
    const first = S.windows.find((w) => w.running);
    location.hash = first ? hp(`term/${encodeURIComponent(first.name)}`) : "#/sessions";
  }
  route();
  startEvents();
  setInterval(() => S.route === "sessions" && !document.hidden && renderCards(), 30000);
  setInterval(() => S.route === "sessions" && !document.hidden && loadOtherHosts(), 5000);
})();
