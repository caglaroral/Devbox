// Leak/CPU probe: keep a page open, sample CDP metrics (after forced GC) every 15 s.
const { chromium } = require("playwright-core");
const fs = require("fs");
// Env (set by tests/test_browser.py): DEVBOX_URL, DEVBOX_COOKIE, CHROME (optional), OUT
const U = process.env.DEVBOX_URL;
const cookie = process.env.DEVBOX_COOKIE;
const launchOpts = process.env.CHROME ? { executablePath: process.env.CHROME } : {};
const DURATION = +(process.env.DUR || 90) * 1000;

const results = [];
async function probe(browser, label, route, mobile) {
  const c = await browser.newContext(mobile ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } : { viewport: { width: 1280, height: 800 } });
  await c.addCookies([{ name: "devbox_auth", value: cookie, url: U }]);
  const p = await c.newPage();
  const cdp = await c.newCDPSession(p);
  await cdp.send("Performance.enable");
  await p.goto(U + "/" + route);
  await p.waitForTimeout(3000);
  const sample = async () => {
    await cdp.send("HeapProfiler.collectGarbage");
    const m = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((x) => [x.name, x.value]));
    return { t: Date.now(), heapMB: +(m.JSHeapUsedSize / 1048576).toFixed(2), nodes: m.Nodes, listeners: m.JSEventListeners, task: m.TaskDuration };
  };
  const rows = [await sample()];
  const t0 = Date.now();
  while (Date.now() - t0 < DURATION) { await p.waitForTimeout(15000); rows.push(await sample()); }
  const cpu = rows.slice(1).map((r, i) => (((r.task - rows[i].task) / ((r.t - rows[i].t) / 1000)) * 100).toFixed(1) + "%");
  console.log(`\n${label}`);
  console.log(" heapMB   :", rows.map((r) => r.heapMB).join(" → "));
  console.log(" nodes    :", rows.map((r) => r.nodes).join(" → "));
  console.log(" listeners:", rows.map((r) => r.listeners).join(" → "));
  console.log(" main-thread CPU per 15s window:", cpu.join(" "));
  results.push({ label, rows, cpu: rows.slice(1).map((r, i) => (r.task - rows[i].task) / ((r.t - rows[i].t) / 1000)) });
  await c.close();
}

(async () => {
  const browser = await chromium.launch(launchOpts);
  await Promise.all([
    probe(browser, "desktop · terminal of a busy remote session (output every second)", "#/h/gpu-box/term/trainer"),
    probe(browser, "desktop · sessions page (cards refresh every 4 s)", "#/h/local/sessions"),
    probe(browser, "mobile · terminal", "#/h/gpu-box/term/trainer", true),
  ]);
  await browser.close();
  // after warm-up (first sample), heap must stay flat and DOM/listener counts constant
  const fail = [];
  for (const r of results) {
    const warm = r.rows.slice(1);
    const growth = warm[warm.length - 1].heapMB - warm[0].heapMB;
    if (growth > 0.5) fail.push(`${r.label}: heap grew ${growth.toFixed(2)} MB`);
    if (new Set(warm.map((x) => x.nodes)).size > 1) fail.push(`${r.label}: DOM nodes changed`);
    if (new Set(warm.map((x) => x.listeners)).size > 1) fail.push(`${r.label}: listeners changed`);
    if (Math.max(...r.cpu) > 0.1) fail.push(`${r.label}: main-thread CPU > 10%`);
  }
  if (fail.length) { console.error("FAIL", fail); process.exit(1); }
})().catch((e) => { console.error(e); process.exit(1); });
