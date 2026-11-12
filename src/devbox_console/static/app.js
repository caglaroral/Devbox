"use strict";
/* devbox console — client. No build step; xterm.js from CDN.
 * A "session" = saved record (name, display, provider, start folder, command) + tmux window running claude.
 * Hosts: "local" (this machine) + remote machines over SSH; every host has its own sessions.
 * Routes: #/h/<host>/sessions  #/h/<host>/term/<name>  #/h/<host>/shell  #/team  #/settings
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
  settings: null, dirs: null,
};

const STATUS_LABEL = { working: "Çalışıyor", waiting: "Onay bekliyor", idle: "Boşta", shell: "Kabuk", dead: "Kapandı", stopped: "Durdu" };
const cur = () => S.windows.find((w) => w.name === S.current);
const hp = (p) => `#/h/${encodeURIComponent(S.hostId)}/${p}`;
const curHost = () => S.hosts.find((h) => h.id === S.hostId);
// endpoints that act on one host get ?host=<id>
const HOST_SCOPED = /^\/(api\/(sessions|windows|dirs|settings|events)|ws\/term)/;
function withHost(path) {
  if (!HOST_SCOPED.test(path)) return path;
  return path + (path.includes("?") ? "&" : "?") + "host=" + encodeURIComponent(S.hostId);
}

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const r = await fetch(withHost(path), {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}
const sessApi = (name, sub = "", opts = {}) => api(`/api/sessions/${encodeURIComponent(name)}${sub}`, opts);

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 2400);
}

/* ------------------------------------------------------------------ clipboard */
async function copyText(text) {
  if (!text) { toast("Seçili metin yok"); return false; }
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // plain http on a LAN address: Clipboard API is unavailable, fall back to execCommand.
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy"); ta.remove();
    if (!ok) { openTextDialog(text, "Kopyalanamadı — buradan seçip kopyalayın"); return false; }
  }
  toast(`Kopyalandı (${text.length} karakter)`);
  return true;
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { term.paste(text); if (!isTouch()) term.focus(); return; }
  } catch { /* permission denied or insecure context */ }
  openPasteDialog("Yapıştır", "Pano okunamadı. Metni buraya yapıştırın (uzun basın → Yapıştır) ve Gönder'e dokunun.");
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

// Ctrl+Shift+C / Ctrl+Shift+V (and Cmd+C/V on mac).
term.attachCustomKeyEventHandler((e) => {
  if (e.type !== "keydown") return true;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey && e.shiftKey && k === "c") || (e.metaKey && k === "c" && term.hasSelection())) {
    copyText(term.getSelection()); e.preventDefault(); return false;
  }
  if ((e.ctrlKey && e.shiftKey && k === "v") || (e.metaKey && k === "v")) {
    e.preventDefault(); pasteFromClipboard(); return false;
  }
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
  overlay("Bağlanıyor…");
  ws = new WebSocket(`${proto}://${location.host}${withHost(`/ws/term?${q}`)}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { wsRetry = 0; overlay(null); term.reset(); wsSend({ t: "resize", c: term.cols, r: term.rows }); if (!isTouch()) term.focus(); };
  ws.onmessage = (ev) => term.write(new Uint8Array(ev.data));
  ws.onclose = () => {
    ws = null;
    if (!wsWanted) return;
    const delay = Math.min(1000 * 2 ** wsRetry++, 15000);
    overlay(`Bağlantı koptu — ${Math.round(delay / 1000)} sn sonra yeniden denenecek`);
    setTimeout(() => { if (wsWanted && isTermRoute()) connect(); }, delay);
  };
}

function showStopped(w) {
  term.reset();
  if (!w) return overlay("Oturum bulunamadı");
  overlay(`<div class="stopped-box"><b>${esc(w.display)}</b><span>Bu oturum durmuş. Klasör: <code>${esc(shortPath(w.cwd))}</code></span>
    <div><button class="btn primary" data-ov="resume">Devam ettir (--continue)</button> <button class="btn" data-ov="fresh">Yeni başlat</button></div></div>`, true);
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
  if (a === "manage") return openManage();
  if (a === "compose") return openPasteDialog("Yaz", "Otomatik düzeltme ve dikte ile rahat yazmak için. Gönder ile terminale aktarılır.", true);
});

/* selection view / paste dialogs */
let textLines = 300;
function openTextDialog(text, title = "Ekran metni") {
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
$("#textCopySel").onclick = () => { const t = taSelection() || String(getSelection() || ""); t ? copyText(t) : toast("Önce metin seçin"); };
$("#textPasteSel").onclick = () => {
  const t = taSelection() || String(getSelection() || "");
  if (!t) return toast("Önce metin seçin");
  term.paste(t); $("#textDlg").close(); toast("Terminale yazıldı");
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
  if (snap.host_id && snap.host_id !== S.hostId) return;
  Object.assign(S, { windows: snap.windows, session: snap.session, host: snap.host, user: snap.user || S.user,
    login: snap.login || snap.user, error: snap.error });
  renderAll();
  // the session we look at was started/stopped elsewhere: (re)connect or show the stopped panel
  if (S.route === "term") {
    const w = cur();
    if (w?.running && (!ws || wsKind !== "tmux")) { overlay(null); connect(); }
    else if (w && !w.running && ws) { closeWs(); showStopped(w); }
  }
}

async function startSession(name, resume = true) {
  try {
    applySnapshot(await sessApi(name, "/start", { method: "POST", body: { resume } }));
    toast(resume ? "Devam ettiriliyor (--continue)" : "Başlatıldı");
    go(hp(`term/${encodeURIComponent(name)}`));
  } catch (e) { toast(e.message); }
}
async function stopSession(name, confirmed = false) {
  if (!confirmed && !confirm(`"${name}" durdurulsun mu? claude kapanır, kayıt kalır; sonra devam ettirebilirsiniz.`)) return false;
  try { applySnapshot(await sessApi(name, "/stop", { method: "POST" })); toast("Durduruldu"); return true; } catch (e) { toast(e.message); return false; }
}
async function deleteSession(name, confirmed = false) {
  if (!confirmed && !confirm(`"${name}" silinsin mi? Çalışıyorsa kapatılır ve kayıt silinir. (Proje klasörüne dokunulmaz.)`)) return false;
  try {
    applySnapshot(await sessApi(name, "", { method: "DELETE" })); toast("Silindi");
    if (S.current === name && S.route === "term") go(hp("sessions"));
    return true;
  } catch (e) { toast(e.message); return false; }
}
function openSession(name) {
  const w = S.windows.find((x) => x.name === name); if (!w) return;
  go(hp(`term/${encodeURIComponent(name)}`));
}

async function hideSession(name, hidden = true) {
  try {
    applySnapshot(await sessApi(name, "/hide", { method: "POST", body: { hidden } }));
    toast(hidden ? "Görünümden kaldırıldı — makinede çalışmaya devam ediyor" : "Geri getirildi");
    return true;
  } catch (e) { toast(e.message); return false; }
}

/* "Kapat…": ask whether to really end it or only remove it from the view */
let clName = null;
function closeAsk(name) {
  const w = S.windows.find((x) => x.name === name); if (!w) return;
  clName = name;
  $("#clTitle").textContent = `${w.display} — kapat`;
  $("#clInfo").textContent = w.running
    ? `${w.external ? "Konsol dışında açılmış tmux penceresi" : "Çalışan oturum"} · ${S.host} · ${shortPath(w.cwd)}`
    : `Durmuş oturum · ${S.host}`;
  const stop = $('#closeDlg [data-cl="stop"]'), del = $('#closeDlg [data-cl="delete"]'), hide = $('#closeDlg [data-cl="hide"]');
  stop.classList.toggle("hidden", !w.running);
  del.classList.toggle("hidden", !!w.external);
  hide.classList.toggle("hidden", !!w.hidden);
  $("#clStopT").textContent = w.external ? "Pencereyi kapat" : "Durdur";
  $("#clStopD").textContent = w.external
    ? "tmux penceresi ve içindeki program sonlanır (kill-window)."
    : 'claude kapanır, kayıt kalır; sonra "Devam" ile kaldığı yerden sürdürebilirsiniz.';
  $("#closeDlg").showModal();
}
$("#closeDlg").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cl]"); if (!b) return;
  const a = b.dataset.cl, name = clName;
  $("#closeDlg").close();
  if (a === "cancel" || !name) return;
  let ok = false;
  if (a === "hide") ok = await hideSession(name, true);
  if (a === "stop") ok = await stopSession(name, true);
  if (a === "delete") ok = await deleteSession(name, true);
  if (ok) { closeEditor(); if (S.current === name && S.route === "term" && a !== "hide") go(hp("sessions")); }
  if (ok && a === "hide" && S.current === name && S.route === "term") go(hp("sessions"));
});

/* manage dialog (inside a terminal) */
function openManage() {
  const w = cur(); if (!w || S.route !== "term") return;
  $("#mgTitle").textContent = `${w.display} (${w.name})`;
  $("#mgSend").value = "";
  $$("#manageDlg [data-mg]").forEach((b) => {
    const needsRun = ["interrupt", "restart", "send", "text"].includes(b.dataset.mg);
    b.disabled = (needsRun && !w.running) || (w.external && ["edit", "restart", "resume"].includes(b.dataset.mg));
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
    if (a === "interrupt") { await winAction({ action: "interrupt" }); toast("Ctrl‑C gönderildi"); }
    if (a === "edit") { dlg.close(); openEditor(w.name); }
    if ((a === "restart" || a === "resume") && w.running) {
      if (!confirm("claude sonlandırılıp aynı klasörde yeniden başlatılsın mı?")) return;
      let cmd = w.command || S.settings?.new_window_command || "claude";
      if (a === "resume" && cmd.startsWith("claude") && !cmd.includes("--continue")) cmd += " --continue";
      await winAction({ action: "respawn", command: cmd }); dlg.close(); toast("Yeniden başlatıldı");
    } else if (a === "resume") { dlg.close(); await startSession(w.name, true); }
    else if (a === "restart") { dlg.close(); await startSession(w.name, false); }
    if (a === "text") { dlg.close(); openScreenText(); }
    if (a === "closeask") { dlg.close(); closeAsk(w.name); }
    if (a === "send" && $("#mgSend").value) {
      await winAction({ action: "send", text: $("#mgSend").value, enter: true }); $("#mgSend").value = ""; toast("Gönderildi");
    }
  } catch (err) { toast(err.message); }
});
$("#mgSend").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $('[data-mg="send"]').click(); } });

/* ------------------------------------------------------------------ session editor (create / update / delete) */
let edName = null; // null = creating

async function ensureSettings() { if (!S.settings) { try { S.settings = await api("/api/settings"); } catch {} } return S.settings; }
async function loadDirs(force = false) {
  if (S.dirs && !force) return S.dirs;
  try { S.dirs = await api("/api/dirs"); } catch { S.dirs = { recent: [], dirs: [], root: "" }; }
  return S.dirs;
}

async function openEditor(name = null) {
  edName = name;
  const w = name ? S.windows.find((x) => x.name === name) : null;
  const st = await ensureSettings();
  $("#edTitle").textContent = w ? (w.saved ? "Oturumu düzenle" : "Oturumu kaydet") : "Yeni oturum";
  if (w) {
    $("#fDisplay").value = w.display === w.name ? "" : w.display;
    $("#fName").value = w.name; $("#fProvider").value = w.provider || "";
    $("#fCwd").value = shortPath(w.cwd || ""); $("#fCmd").value = w.command ?? "";
    $("#fAutostart").checked = !!w.autostart;
  } else {
    const used = new Set(S.windows.map((x) => x.name));
    let n = 1; while (used.has(`claude-${n}`)) n++;
    $("#fDisplay").value = ""; $("#fName").value = `claude-${n}`; $("#fProvider").value = "";
    $("#fCmd").value = st?.new_window_command ?? "claude"; $("#fAutostart").checked = false;
    $("#fCwd").value = "";
  }
  $("#edDelete").classList.toggle("hidden", !w);
  $("#edSaveStart").textContent = !w ? "Oluştur ve başlat" : w.running ? "Kaydet" : "Kaydet ve başlat";
  $("#edSave").classList.toggle("hidden", !!w?.running);
  $("#edSave").textContent = w ? "Kaydet" : "Sadece kaydet";
  $("#app").classList.add("editor-open"); $("#editor").setAttribute("aria-hidden", "false");
  const d = await loadDirs(true);
  if (!w && !$("#fCwd").value) $("#fCwd").value = d.recent[0] ? shortPath(d.recent[0]) : (d.root || "");
  renderDirPick();
  if (!isTouch()) (w ? $("#fDisplay") : $("#fCwd")).focus();
}
function closeEditor() { $("#app").classList.remove("editor-open"); $("#editor").setAttribute("aria-hidden", "true"); }
$("#edClose").onclick = closeEditor;

function renderDirPick() {
  const d = S.dirs || { recent: [], dirs: [] };
  const q = $("#fCwd").value.trim().toLowerCase();
  const recent = d.recent.map((p) => ({ p, r: true }));
  const pool = [...recent, ...d.dirs.filter((p) => !d.recent.includes(p)).map((p) => ({ p }))];
  const exact = (p) => shortPath(p).toLowerCase() === q || p.toLowerCase() === q;
  const list = (q ? pool.filter(({ p }) => p.toLowerCase().includes(q.replace(/^~/, "")) && !exact(p)) : pool).slice(0, 8);
  $("#dirPick").innerHTML = list.map(({ p, r }) => {
    const sp = shortPath(p), cut = sp.lastIndexOf("/");
    return `<button type="button" data-dir="${esc(p)}"><span class="ico">${r ? "↺" : "▸"}</span><span class="dp-parent">${esc(sp.slice(0, cut + 1))}</span><b>${esc(sp.slice(cut + 1))}</b></button>`;
  }).join("") || (q ? `<div class="muted small">Eşleşen klasör yok — tam yolu yazabilirsiniz.</div>` : "");
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
  const name = $("#fName").value.trim();
  try {
    if (!edName) {
      const snap = await api("/api/sessions", { method: "POST", body: { ...body, name, start } });
      applySnapshot(snap); closeEditor();
      toast(start ? "Oturum açıldı — claude başlatıldı" : "Kaydedildi");
      if (start) openSession(name);
    } else {
      const was = S.windows.find((x) => x.name === edName);
      applySnapshot(await sessApi(edName, "", { method: "PATCH", body: { ...body, new_name: name } }));
      if (S.current === edName) { S.current = name; history.replaceState(null, "", hp(`term/${encodeURIComponent(name)}`)); }
      closeEditor(); toast("Kaydedildi");
      if (start && was && !was.running) await startSession(name, true);
    }
  } catch (err) { toast(err.message); }
});
$("#edDelete").onclick = () => closeAsk(edName);

/* ------------------------------------------------------------------ hosts (local + remote machines) */
function switchHost(hid) {
  if (hid === S.hostId) return;
  closeWs();
  S.hostId = hid; S.windows = []; S.current = null; S.dirs = null; S.settings = null; S.error = null;
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
  if (h.id === S.hostId) return S.windows.map((w) => ({ name: w.name, display: w.display, status: w.status, running: w.running, cwd: w.cwd, hidden: w.hidden }));
  return h.sessions || [];
}
function renderHosts() {
  const list = S.hosts.length ? S.hosts : [{ id: "local", label: S.host, remote: false }];
  setHTML($("#hostList"), list.map((h) => {
    const all = hostSessions(h);
    const sess = all.filter((x) => !x.hidden), nHidden = all.length - sess.length;
    const busy = all.filter((x) => x.status === "working").length;
    return `<li class="host-row ${h.id === S.hostId ? "cur" : ""}" data-host="${esc(h.id)}" title="${esc(h.error || `${h.username || ""}@${h.hostname || ""}`)}">
      <span class="hico">${h.remote ? "⇄" : "⌂"}</span><span class="nm">${esc(h.label)}</span>
      <span class="dot ${hostDot(h)}" title="${h.error ? "bağlantı yok" : "bağlı"}"></span><span class="tag" title="çalışan / toplam">${busy ? `<b class="busy">${busy}</b>/` : ""}${sess.length}</span>
      ${h.remote ? `<button class="icon-btn edit" data-hedit="${esc(h.id)}" title="Makineyi düzenle">✎</button>` : ""}
    </li>` + (sess.length ? sess.map((x) => `
    <li class="sess s-${x.status} ${x.running ? "" : "off"} ${S.route === "term" && h.id === S.hostId && x.name === S.current ? "active" : ""}"
        data-host="${esc(h.id)}" data-name="${esc(x.name)}" title="${esc(STATUS_LABEL[x.status] || x.status)}${x.cwd ? " · " + esc(shortPath(x.cwd)) : ""}">
      <span class="dot"></span><span class="nm">${esc(x.display)}</span>
    </li>`).join("") : `<li class="empty-sess">${h.error ? "bağlanılamıyor" : "oturum yok"}</li>`)
      + (nHidden ? `<li class="more" data-host="${esc(h.id)}" data-hidden="1">+${nHidden} gizli (çalışıyor)</li>` : "");
  }).join(""));
}
$("#hostList").addEventListener("click", (e) => {
  const ed = e.target.closest("[data-hedit]");
  if (ed) { e.stopPropagation(); return openHostEditor(ed.dataset.hedit); }
  const li = e.target.closest("[data-host]"); if (!li) return;
  const h = encodeURIComponent(li.dataset.host);
  if (li.dataset.hidden) { setFilter("hidden"); return go(`#/h/${h}/sessions`); }
  go(li.dataset.name ? `#/h/${h}/term/${encodeURIComponent(li.dataset.name)}` : `#/h/${h}/sessions`);
});
$("#newHostBtn").onclick = () => openHostEditor();

function renderHostBanner() {
  const h = curHost(), el = $("#hostBanner");
  if (!h || !h.remote) { el.classList.add("hidden"); return; }
  const i = h.info || {};
  const miss = [!i.tmux && "tmux", !i.claude && "claude"].filter(Boolean);
  el.className = miss.length || h.error ? "warn" : "";
  el.innerHTML = `<span>⇄ <b>${esc(h.label)}</b> · <code>${esc(h.username)}@${esc(h.hostname)}${h.port && h.port !== 22 ? ":" + h.port : ""}</code></span>
    <span class="muted">${esc(i.os || "")}</span>
    ${i.tmux ? `<span class="pill">${esc(i.tmux)}</span>` : ""}${i.claude ? `<span class="pill">claude ${esc(i.claude.split(" ")[0])}</span>` : ""}
    ${miss.length ? `<span>⚠ Eksik: <b>${miss.join(", ")}</b> — SSH ile bağlanıp kurun.</span>` : ""}
    <span class="grow"></span><button class="btn" data-hb="ssh">SSH aç</button><button class="btn" data-hb="edit">Düzenle</button>`;
}
$("#hostBanner").addEventListener("click", (e) => {
  const a = e.target.dataset?.hb;
  if (a === "ssh") go(hp("shell"));
  if (a === "edit") openHostEditor(S.hostId);
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
  $("#hTitle").textContent = h ? "Makineyi düzenle" : "Uzak makine ekle";
  $("#hLabel").value = h?.label || ""; $("#hHostname").value = h?.hostname || "";
  $("#hPort").value = h?.port || 22; $("#hUser").value = h?.username || "";
  $("#hPassword").value = ""; $("#hKey").value = ""; $("#hStorePw").checked = h?.auth === "password";
  setAuthPane(h ? "manual" : "password");
  const AUTH_LABEL = { "custom-key": "yapıştırılmış özel anahtar", "password": "Vault'ta saklanan parola", "console-key": "konsol anahtarı" };
  $("#hAuthNow").textContent = h ? `Şu an: ${AUTH_LABEL[h.auth] || h.auth}. Değiştirmek için parola ya da yeni anahtar girin.` : "";
  $("#hDelete").classList.toggle("hidden", !h);
  $("#hTest").classList.toggle("hidden", !h);
  $("#hShell").classList.toggle("hidden", !h);
  $("#hSave").textContent = h ? "Kaydet" : "Kaydet ve bağlan";
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
  $("#hInfo").innerHTML = (h?.error ? row("Durum", h.error, true) : "") + (info ? [
    row("Hostname", info.host || "?"), row("Sistem", info.os || "?"),
    row("tmux", info.tmux || "kurulu değil", !info.tmux), row("claude", info.claude || "kurulu değil / PATH'te yok", !info.claude),
  ].join("") : "");
}

$("#hostForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { label: $("#hLabel").value.trim(), hostname: $("#hHostname").value.trim(),
                 port: +$("#hPort").value || 22, username: $("#hUser").value.trim() };
  if (hAuth === "password" && $("#hPassword").value) { body.password = $("#hPassword").value; body.store_password = $("#hStorePw").checked; }
  if (hAuth === "manual") body.auth = "manual";
  if (hAuth === "key" && $("#hKey").value.trim()) body.private_key = $("#hKey").value.trim();
  if (!hEditing && hAuth === "password" && !body.password) return toast("Parola girin (ya da Elle/Özel anahtar seçin)");
  const btn = $("#hSave"); btn.disabled = true; btn.textContent = "Bağlanıyor…";
  try {
    if (hEditing) {
      S.hosts = (await api(`/api/hosts/${encodeURIComponent(hEditing)}`, { method: "PATCH", body })).hosts;
      toast("Kaydedildi"); closeHostEditor();
      if (hEditing === S.hostId) { const id = S.hostId; S.hostId = ""; switchHost(id); }
    } else {
      const r = await api("/api/hosts", { method: "POST", body });
      S.hosts = r.hosts; closeHostEditor();
      const i = r.host.info || {};
      toast(`Bağlandı: ${r.host.label}` + (!i.tmux || !i.claude ? " — eksik kurulum var" : ""));
      go(`#/h/${encodeURIComponent(r.host.id)}/sessions`);
    }
    renderHosts();
  } catch (err) { toast(err.message); }
  btn.disabled = false; btn.textContent = hEditing ? "Kaydet" : "Kaydet ve bağlan";
});
$("#hTest").onclick = async () => {
  const b = $("#hTest"); b.disabled = true; b.textContent = "Test ediliyor…";
  try {
    const info = await api(`/api/hosts/${encodeURIComponent(hEditing)}/test`, { method: "POST" });
    renderHostInfo(null, info); toast(info.ok ? "Bağlantı tamam" : "Bağlandı ama komut hata verdi");
    S.hosts = (await api("/api/hosts")).hosts; renderHosts();
  } catch (err) { renderHostInfo({ error: err.message }); toast("Bağlantı başarısız"); }
  b.disabled = false; b.textContent = "Bağlantıyı test et";
};
$("#hShell").onclick = () => { const id = hEditing; closeHostEditor(); go(`#/h/${encodeURIComponent(id)}/shell`); };
$("#hDelete").onclick = async () => {
  const h = S.hosts.find((x) => x.id === hEditing);
  if (!confirm(`"${h?.label}" listeden silinsin mi?\nUzak makinedeki tmux oturumlarına dokunulmaz; kayıtlı oturum tanımları da bu konsolda kalır.`)) return;
  try {
    S.hosts = (await api(`/api/hosts/${encodeURIComponent(hEditing)}`, { method: "DELETE" })).hosts;
    closeHostEditor(); toast("Silindi");
    if (S.hostId === hEditing) go("#/h/local/sessions"); else renderHosts();
  } catch (err) { toast(err.message); }
};

/* ------------------------------------------------------------------ routing */
function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }

function route() {
  let parts = location.hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "h") {
    const hid = decodeURIComponent(parts[1] || "local");
    if (hid !== S.hostId) switchHost(hid);
    parts = parts.slice(2);
  }
  const [page, arg] = parts;
  const r = ["sessions", "team", "settings", "term", "shell"].includes(page) ? page : "sessions";
  const wantKind = r === "shell" ? "shell" : r === "term" ? "tmux" : null;
  if (ws && wantKind !== wsKind) closeWs();
  if (r === "term") {
    const name = decodeURIComponent(arg || "");
    if (S.windows.length && !S.windows.find((w) => w.name === name)) { go(hp("sessions")); return; }
    if (!S.windows.length) { S.current = name; S.route = "term"; }
    const switching = S.current !== name;
    S.current = name;
    const w = cur();
    if (switching && w?.running && ws && ws.readyState === 1 && w.tmux_session === wsSess) { wsSend({ t: "select", w: w.id }); if (!isTouch()) term.focus(); }
    else if (switching && ws) closeWs();
  }
  S.route = r;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${r === "shell" ? "term" : r}`));
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === r));
  $("[data-nav=sessions]").setAttribute("href", hp("sessions"));
  $("#app").classList.remove("menu-open");
  if (isTermRoute()) requestAnimationFrame(() => { fit.fit(); if (!ws) connect(); });
  else closeWs();
  if (r === "team") { S.unread = 0; renderUnread(); renderBoard(true); }
  if (r === "settings") loadSettings();
  renderAll();
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ rendering */
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "az önce";
  if (s < 3600) return `${Math.floor(s / 60)} dk`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa`;
  return `${Math.floor(s / 86400)} g`;
}
// replace innerHTML only when it really changed: periodic refreshes must not eat taps/clicks
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
const shortPath = (p) => (p || "").replace(/^\/home\/[^/]+(?=\/|$)/, "~").replace(/^\/root(?=\/|$)/, "~");
const initials = (s) => (s || "?").replace(/\(.*?\)/g, "").trim().split(/[\s._-]+/).map((x) => x[0]).join("").slice(0, 2).toUpperCase() || "?";

function renderAll() {
  if (document.hidden) { renderAll.pending = true; return; }
  renderSidebar(); renderHead(); renderCards(); renderTargets();
}

function renderSidebar() {
  const running = S.windows.filter((w) => w.running);
  $("#sessionCount").textContent = S.windows.length;
  $("#shellItem li").classList.toggle("active", S.route === "shell");
  $("#shellItem .nm").textContent = S.hostId === "local" ? "Shell" : `SSH · ${S.host}`;
  $("#whoUser").textContent = S.user || "";
  $("#whoHost").textContent = `${S.hosts.length || 1} host · ${running.length} çalışıyor burada`;
  renderHosts();
  $("#avatar").textContent = (S.user || "?")[0].toUpperCase();
}

function renderHead() {
  $("#manageBtn").classList.toggle("hidden", S.route === "shell");
  if (S.route === "shell") {
    const remote = S.hostId !== "local";
    $("#tDisplay").textContent = remote ? `SSH · ${S.host}` : "Shell"; $("#tName").textContent = `${S.login}@${S.host}`;
    $("#tStatus").className = "status s-shell"; $("#tStatus").innerHTML = `<span class="dot"></span>${remote ? "ssh login shell (tmux dışı)" : "login shell (tmux dışı)"}`;
    $("#tProvider").textContent = ""; $("#tPath").textContent = "";
    document.title = `${remote ? "SSH" : "Shell"} · ${S.host} · devbox`;
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

function renderCards() {
  $("#errorBox").classList.toggle("hidden", !S.error);
  $("#errorBox").textContent = S.error || "";
  $("#vaultHost").textContent = S.hosts.length > 1 || S.hostId !== "local" ? `· ${S.host}` : "";
  renderHostBanner();
  const q = $("#sessFilter").value.trim().toLowerCase();
  const nHidden = S.windows.filter((w) => w.hidden).length;
  $("#hiddenSeg").textContent = nHidden ? `Gizli (${nHidden})` : "Gizli";
  const list = S.windows.filter((w) =>
    (S.filter === "hidden" ? w.hidden : !w.hidden && (S.filter === "all" || (S.filter === "running") === !!w.running)) &&
    (!q || [w.display, w.name, w.cwd, w.provider, w.command].join(" ").toLowerCase().includes(q)));
  const stopped = S.windows.filter((w) => !w.running && !w.hidden).length;
  $("#startAllBtn").classList.toggle("hidden", !stopped);
  setHTML($("#cards"), list.length ? list.map((w) => `
    <div class="card s-${w.status} ${w.running ? "" : "off"} ${w.hidden ? "hidden-card" : ""}" data-name="${esc(w.name)}">
      <div class="card-head">
        <span class="tile">${esc(initials(w.display))}<span class="dot"></span></span>
        <div class="card-title"><b>${esc(w.display)}</b><span class="muted small"><code>${esc(w.name)}</code> · ${esc(STATUS_LABEL[w.status] || w.status)}${w.saved || w.external ? "" : " · kaydedilmemiş"}</span></div>
        ${w.external ? "" : `<button class="icon-btn" data-act="edit" title="Düzenle">✎</button>`}
        <button class="icon-btn" data-act="close" title="Kapat / gizle">✕</button>
      </div>
      <div class="card-path" title="${esc(w.cwd)}">▭ ${esc(shortPath(w.cwd) || "—")}</div>
      ${w.running ? `<pre>${esc((w.preview || []).join("\n"))}</pre>` : ""}
      <div class="card-foot">
        ${w.external ? `<span class="pill ext" title="Konsol dışında açılmış tmux oturumu: ${esc(w.tmux_session)}">harici · ${esc(w.tmux_session)}</span>` : ""}
        ${w.hidden ? `<button class="btn" data-act="unhide">Geri getir</button>` : ""}
        ${w.provider ? `<span class="pill">✦ ${esc(w.provider)}</span>` : ""}
        <code class="cmd">${esc(w.command || w.cmd || "")}</code>
        ${w.autostart ? '<span class="pill" title="Otomatik başlar">auto</span>' : ""}
        <span class="grow"></span>
        <span class="muted">${ago(w.last_change)}</span>
        ${w.running
          ? (w.status === "dead" ? `<button class="btn" data-act="restart">Yeniden başlat</button>` : "") + `<button class="btn primary" data-act="open">Bağlan</button>`
          : `<button class="btn" data-act="fresh" title="Yeni konuşma">Başlat</button><button class="btn primary" data-act="resume" title="claude --continue">Devam</button>`}
      </div>
    </div>`).join("")
    : `<div class="empty">${S.windows.length ? "Eşleşen oturum yok." : "Henüz oturum yok.<br><br><button class='btn primary' data-act='new'>＋ İlk oturumu oluştur</button><br><br><span class='small'>Her oturum: seçtiğiniz klasörde tmux içinde çalışan bir claude.</span>"}</div>`);
}

function renderTargets() {
  const tgt = (w) => (S.hostId === "local" ? w.name : `${w.name}@${S.hostId}`);
  const opts = S.windows.map((w) => `<option value="${esc(tgt(w))}">${esc(w.display)} (${esc(tgt(w))})</option>`).join("");
  const sets = [[$("#msgTo"), `<option value="all">Herkes</option>${opts}`],
                [$("#teamFilter"), `<option value="">Tümü</option><option value="user">${esc(S.user || "Kullanıcı")}</option>${opts}`]];
  for (const [sel, html] of sets) {
    if (sel.dataset.html === html) continue;
    const v = sel.value; sel.innerHTML = html; sel.dataset.html = html;
    sel.value = [...sel.options].some((o) => o.value === v) ? v : sel.options[0].value;
  }
}

$("#shellItem").addEventListener("click", () => go(hp("shell")));
$("#cards").addEventListener("click", async (e) => {
  const actBtn = e.target.closest("[data-act]");
  const card = e.target.closest(".card");
  const act = actBtn?.dataset.act;
  if (act === "new") return openEditor();
  if (!card) return;
  const name = card.dataset.name, w = S.windows.find((x) => x.name === name);
  if (act === "edit") return openEditor(name);
  if (act === "close") return closeAsk(name);
  if (act === "unhide") return hideSession(name, false);
  if (act === "resume") return startSession(name, true);
  if (act === "fresh") return startSession(name, false);
  if (act === "restart") {
    try { await api(`/api/windows/${encodeURIComponent(w.id)}/action`, { method: "POST", body: { action: "respawn", command: w.command || "claude" } }); toast("Yeniden başlatıldı"); }
    catch (err) { toast(err.message); }
    return;
  }
  if (act === "open" || w?.running) return openSession(name);
  openEditor(name); // a stopped card: show its details
});
$("#sessFilter").addEventListener("input", renderCards);
function setFilter(f) {
  S.filter = f; $$("#sessView button").forEach((x) => x.classList.toggle("on", x.dataset.f === f)); renderCards();
}
$("#sessView").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) setFilter(b.dataset.f); });
$("#refreshBtn").onclick = async () => { applySnapshot(await api("/api/windows?refresh=1")); toast("Güncellendi"); };
$("#startAllBtn").onclick = async () => {
  const stopped = S.windows.filter((w) => !w.running && !w.hidden);
  for (const w of stopped) {
    try { applySnapshot(await sessApi(w.name, "/start", { method: "POST", body: { resume: true } })); } catch (e) { toast(`${w.name}: ${e.message}`); }
  }
  toast(`${stopped.length} oturum başlatıldı`);
};
$("#newSessionBtn").onclick = () => openEditor();
$("#newSessionBtn2").onclick = () => openEditor();

/* ------------------------------------------------------------------ team */
function fmtText(t) { return esc(t).replace(/`([^`\n]+)`/g, "<code>$1</code>"); }
function fmtTime(ts) {
  const d = new Date(ts * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" }) : d.toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" });
}
function nameOf(n) {
  if (n === "user") return S.user || "Kullanıcı";
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
        <span>${fmtTime(m.ts)}</span>${m.delivered?.length ? `<span class="dlv" title="send-keys ile iletildi">✓ ${esc(m.delivered.join(", "))}</span>` : ""}</div>
      <div class="msg-body">${fmtText(m.text)}</div>
    </div>`).join("") : `<div class="empty">Henüz mesaj yok.<br><span class="small">Oturumlar <code>devbox-msg post "…"</code> ile yazar, <code>devbox-msg read</code> ile okur.</span></div>`;
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
      <td><button class="btn" data-edit="${esc(e.name)}">Değiştir</button> <button class="btn danger" data-del="${esc(e.name)}">Sil</button></td></tr>`).join("")
    || `<tr><td class="muted">Tanımlı değişken yok.</td></tr>`;
  $("#newCmdDefault").value = s.new_window_command || "";
  $("#projectsRoot").value = s.projects_root || "";
  $("#pubUrl").textContent = s.public_url;
  $("#apiToken").textContent = s.token;
  $("#tmuxInfo").textContent = `${s.user} · ${s.host} · session ${s.session}`;
  $("#rootHost").textContent = s.host;
  $("#pubKey").textContent = s.public_key || "";
  const st = s.secrets || {};
  $("#storeInfo").textContent = st.kind === "vault" ? `HashiCorp Vault · ${st.location} · ${st.mount}/ (${st.mode === "managed" ? "konsol yönetiyor" : "harici"})` : `dosya (0600) · ${st.location}`;
  $("#msgInstall").textContent = `mkdir -p ~/.local/bin && curl -fsS ${s.public_url}/devbox-msg -o ~/.local/bin/devbox-msg && chmod +x ~/.local/bin/devbox-msg`;
}
$("#saveDefaults").onclick = async () => {
  try {
    await api("/api/settings", { method: "PUT", body: { new_window_command: $("#newCmdDefault").value.trim(), projects_root: $("#projectsRoot").value.trim() } });
    S.settings = null; S.dirs = null; toast("Kaydedildi");
  } catch (err) { toast(err.message); }
};
$("#envForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/env", { method: "PUT", body: { name: $("#envName").value.trim(), value: $("#envValue").value } });
    $("#envName").value = ""; $("#envValue").value = ""; toast("Kaydedildi — yeni/yeniden başlatılan oturumlarda geçerli"); loadSettings();
  } catch (err) { toast(err.message); }
});
$("#envRows").addEventListener("click", async (e) => {
  if (e.target.dataset.edit) { $("#envName").value = e.target.dataset.edit; $("#envValue").focus(); }
  if (e.target.dataset.del && confirm(`${e.target.dataset.del} silinsin mi?`)) {
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
    ...S.windows.map((w) => ({ label: w.display + (w.hidden ? " (gizli)" : ""), k: `${w.name} · ${STATUS_LABEL[w.status] || ""} · ${shortPath(w.cwd)}`, dot: w.status, run: () => openSession(w.name) })),
    { label: S.hostId === "local" ? "Shell" : `SSH · ${S.host}`, k: `${S.login}@${S.host}`, run: () => go(hp("shell")) },
    ...S.hosts.filter((h) => h.id !== S.hostId).map((h) => ({ label: `Host: ${h.label}`, k: `${h.username}@${h.hostname}`, run: () => go(`#/h/${encodeURIComponent(h.id)}/sessions`) })),
    { label: "Uzak makine ekle…", k: "komut", run: () => openHostEditor() },
    { label: "Yeni oturum…", k: "komut", run: () => openEditor() },
    { label: "Sessions", k: "sayfa", run: () => go(hp("sessions")) },
    { label: "Team", k: "sayfa", run: () => go("#/team") },
    { label: "Settings", k: "sayfa", run: () => go("#/settings") },
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
  es.addEventListener("hosts", (e) => { S.hosts = JSON.parse(e.data); renderHosts(); renderHostBanner(); });
  es.addEventListener("message", (e) => addMessage(JSON.parse(e.data)));
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (renderAll.pending) { renderAll.pending = false; renderAll(); }
  if (isTermRoute() && !ws) connect();
});

(async function init() {
  const m = location.hash.match(/^#\/h\/([^/]+)/);
  if (m) S.hostId = decodeURIComponent(m[1]);
  try { S.hosts = (await api("/api/hosts")).hosts; } catch {}
  if (!S.hosts.some((h) => h.id === S.hostId)) S.hostId = "local";
  try { applySnapshot(await api("/api/windows")); } catch {}
  ensureSettings();
  loadMessages().catch(() => {});
  if (!location.hash) {
    const first = S.windows.find((w) => w.running);
    location.hash = first ? hp(`term/${encodeURIComponent(first.name)}`) : hp("sessions");
  }
  route();
  startEvents();
  setInterval(() => S.route === "sessions" && !document.hidden && renderCards(), 30000);
})();
