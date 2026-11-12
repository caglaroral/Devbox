// Browser smoke test: desktop + mobile screenshots, JS errors.
const { chromium } = require("playwright-core");
const fs = require("fs");
// Env (set by tests/test_browser.py): DEVBOX_URL, DEVBOX_COOKIE, CHROME (optional), OUT
const U = process.env.DEVBOX_URL;
const cookie = process.env.DEVBOX_COOKIE;
const launchOpts = process.env.CHROME ? { executablePath: process.env.CHROME } : {};
const OUT = process.env.OUT || __dirname + "/shots";
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch(launchOpts);
  const errors = [];
  async function ctx(opts) {
    const c = await browser.newContext(opts);
    await c.addCookies([{ name: "devbox_auth", value: cookie, url: U }]);
    const p = await c.newPage();
    p.on("pageerror", (e) => errors.push(`${opts.tag}: pageerror ${e.message}`));
    p.on("console", (m) => { if (m.type() === "error") errors.push(`${opts.tag}: console ${m.text()}`); });
    return p;
  }
  const shot = async (p, name) => { await p.waitForTimeout(700); await p.screenshot({ path: `${OUT}/${name}.png` }); };

  // ---- desktop
  const d = await ctx({ viewport: { width: 1280, height: 800 }, tag: "desktop" });
  await d.goto(U + "/#/h/local/sessions"); await d.waitForTimeout(1500);
  await shot(d, "d1-sessions");
  await d.click('#hostList li.sess[data-name="joseph"]'); await d.waitForTimeout(1500);
  await shot(d, "d2-term");
  await d.click('#hostList li.sess[data-host="gpu-box"][data-name="trainer"]'); await d.waitForTimeout(2500);
  await shot(d, "d3-remote-term");
  await d.click("#newSessionBtn"); await d.waitForTimeout(800);
  await d.fill("#fCwd", "proj"); await shot(d, "d4-editor");
  await d.keyboard.press("Escape");
  await d.hover('#hostList li.host-row[data-host="gpu-box"]'); await d.click('[data-hedit="gpu-box"]'); await shot(d, "d5-host-editor");
  await d.keyboard.press("Escape");
  // close dialog -> hide -> bring back from the hidden list
  await d.goto(U + "/#/h/local/sessions"); await d.waitForTimeout(1200);
  await d.click('.card[data-name="waiter"] [data-act="close"]'); await shot(d, "d8-close-dialog");
  await d.click('#closeDlg [data-cl="hide"]'); await d.waitForTimeout(800);
  const visible = await d.locator('#hostList li.sess[data-name="waiter"]').count();
  const more = await d.locator('#hostList li.more').count();
  await d.click('#hostList li.more'); await d.waitForTimeout(600); await shot(d, "d9-hidden-list");
  await d.click('.card[data-name="waiter"] [data-act="unhide"]'); await d.waitForTimeout(800);
  const back = await d.locator('#hostList li.sess[data-name="waiter"]').count();
  console.log(`hide flow: in tree after hide=${visible}, "gizli" link=${more}, back after unhide=${back}`);
  await d.goto(U + "/#/hosts"); await d.waitForTimeout(1000); await shot(d, "d10-hosts");
  const hostCards = await d.locator("#hostCards .host-card").count();
  console.log("hosts page cards:", hostCards);
  if (hostCards < 2) errors.push(`hosts page shows ${hostCards} cards, expected local + gpu-box`);
  await d.click('#hostCards [data-hid="gpu-box"] [data-ha="edit"]'); await d.waitForTimeout(500); await shot(d, "d11-hosts-edit");
  if (!(await d.locator("#app.host-open").count())) errors.push("edit on the hosts page did not open the host editor");
  await d.keyboard.press("Escape");
  await d.goto(U + "/#/settings"); await d.waitForTimeout(800); await shot(d, "d6-settings");
  await d.goto(U + "/#/team"); await d.waitForTimeout(800); await shot(d, "d7-team");

  // ---- mobile (iPhone-ish)
  const m = await ctx({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, tag: "mobile" });
  await m.goto(U + "/#/h/local/sessions"); await m.waitForTimeout(1500);
  await shot(m, "m1-sessions");
  await m.click(".view.active [data-menu]"); await shot(m, "m2-drawer-tree");
  await m.click('#hostList li.sess[data-host="gpu-box"][data-name="trainer"]'); await m.waitForTimeout(2500);
  await shot(m, "m3-term");
  await m.click('#keybar [data-action="manage"]'); await shot(m, "m4-manage");
  await m.keyboard.press("Escape");
  await m.goto(U + "/#/h/local/sessions"); await m.waitForTimeout(1200);
  await m.click("#newSessionBtn2"); await m.waitForTimeout(800); await shot(m, "m5-editor");
  await m.click("#edClose");
  await m.click(".view.active [data-menu]"); await m.waitForTimeout(300);
  await m.click("#newHostBtn"); await shot(m, "m6-host-editor");
  // horizontal overflow check on each main view
  const overflow = [];
  for (const route of ["#/h/local/sessions", "#/hosts", "#/team", "#/settings"]) {
    await m.goto(U + "/" + route); await m.waitForTimeout(700);
    const w = await m.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
    if (w[0] > w[1]) overflow.push(`${route}: ${w[0]} > ${w[1]}`);
  }
  for (const route of ["#/h/local/sessions", "#/hosts", "#/team", "#/settings"]) {
    await d.goto(U + "/" + route); await d.waitForTimeout(500);
    const tr = await d.evaluate(() => (document.body.innerText.match(/[^\n]*[çğıöşüÇĞİÖŞÜ][^\n]*/g) || []).slice(0, 3));
    if (tr.length) errors.push(`${route}: non-English text: ${tr.join(" | ")}`);
  }
  console.log("overflow:", overflow.length ? overflow : "none");
  console.log("errors:", errors.length ? errors : "none");
  await browser.close();
  const fail = [...overflow, ...errors];
  if (visible !== 0 || more < 1 || back !== 1) fail.push(`hide flow: after hide=${visible} gizli-link=${more} after unhide=${back}`);
  if (fail.length) { console.error("FAIL", fail); process.exit(1); }
})().catch((e) => { console.error(e); process.exit(1); });
