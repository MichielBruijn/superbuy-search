const BRIDGE = "http://127.0.0.1:8765";
const HDR = { "x-superbuy-bridge": "1" };
const VERSION = browser.runtime.getManifest().version;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function post(path, data) {
  return fetch(BRIDGE + path, {
    method: "POST",
    headers: { ...HDR, "content-type": "application/json" },
    body: JSON.stringify(data),
  });
}

async function superbuyTabs() {
  return browser.tabs.query({ url: "https://www.superbuy.com/*" });
}

// Bestaande Superbuy-tab, anders eentje op de achtergrond openen.
async function getTab() {
  const tabs = (await superbuyTabs()).filter((t) => !t.discarded);
  const done = tabs.find((t) => t.status === "complete");
  if (done) return done;
  if (tabs.length) return waitLoaded(tabs[0].id);
  const t = await browser.tabs.create({ url: "https://www.superbuy.com/en/", active: false });
  return waitLoaded(t.id);
}

function waitLoaded(tabId) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { cleanup(); reject(new Error("Superbuy-tab laadt niet (captcha?)")); }, 45000);
    const fn = (id, info, tab) => {
      if (id === tabId && info.status === "complete") { cleanup(); resolve(tab); }
    };
    const cleanup = () => { clearTimeout(timer); browser.tabs.onUpdated.removeListener(fn); };
    browser.tabs.onUpdated.addListener(fn);
    browser.tabs.get(tabId).then((t) => { if (t.status === "complete") { cleanup(); resolve(t); } });
  });
}

async function runJob(job) {
  let res;
  try {
    const tab = await getTab();
    try {
      res = await browser.tabs.sendMessage(tab.id, job);
    } catch {
      // tab was al open vóór installatie van de extensie: script alsnog injecteren
      await browser.tabs.executeScript(tab.id, { file: "content.js" });
      res = await browser.tabs.sendMessage(tab.id, job);
    }
    if (res && /text\/html/.test(res.contentType || "") && /cf_chl|Just a moment/.test(res.body || "")) {
      res = { id: job.id, ok: false, status: res.status, error: "Cloudflare-check: los hem op in de Superbuy-tab" };
      browser.tabs.update(tab.id, { active: true });
    }
  } catch (e) {
    res = { id: job.id, ok: false, error: String(e) };
  }
  await post("/ext/result", res || { id: job.id, ok: false, error: "geen antwoord van content script" });
}

// Werkbalkknop: badge "!" als de bridge niet bereikbaar is.
function setOnline(online) {
  browser.browserAction.setBadgeText({ text: online ? "" : "!" });
  browser.browserAction.setBadgeBackgroundColor({ color: "#dc2626" });
  browser.browserAction.setTitle({ title: online ? "Superbuy Zoeker openen" : "Superbuy Zoeker: bridge draait niet" });
}

browser.browserAction.onClicked.addListener(async () => {
  const [tab] = await browser.tabs.query({ url: BRIDGE + "/*" });
  if (tab) {
    await browser.tabs.update(tab.id, { active: true });
    await browser.windows.update(tab.windowId, { focused: true });
  } else {
    await browser.tabs.create({ url: BRIDGE + "/" });
  }
});

async function pollLoop() {
  for (;;) {
    try {
      const n = (await superbuyTabs()).length;
      const r = await fetch(`${BRIDGE}/ext/poll?tabs=${n}&v=${VERSION}`, { headers: HDR, cache: "no-store" });
      setOnline(true);
      if (r.status === 200) runJob(await r.json());
      else if (r.status !== 204) await sleep(5000);
    } catch {
      setOnline(false);
      await sleep(5000); // bridge draait niet
    }
  }
}

// --- passief meeloggen van Superbuy-API-verkeer (om endpoints te leren) ---

const reqBodies = new Map();
const reqHeaders = new Map();
const SKIP_HDR = /^(cookie|user-agent|accept-language|accept-encoding|sec-|connection|host|te$|priority)/i;

browser.webRequest.onSendHeaders.addListener(
  (d) => reqHeaders.set(d.requestId, Object.fromEntries(
    d.requestHeaders.filter((h) => !SKIP_HDR.test(h.name)).map((h) => [h.name, h.value]))),
  { urls: ["https://*.superbuy.com/*"], types: ["xmlhttprequest"] },
  ["requestHeaders"]
);

function decodeBody(rb) {
  if (!rb) return null;
  if (rb.formData) return new URLSearchParams(Object.entries(rb.formData).flatMap(([k, vs]) => vs.map((v) => [k, v]))).toString();
  if (rb.raw) return rb.raw.map((p) => (p.bytes ? new TextDecoder().decode(p.bytes) : "")).join("");
  return null;
}

browser.webRequest.onBeforeRequest.addListener(
  (d) => {
    if (d.url.match(/\.(js|css|png|jpe?g|gif|webp|svg|woff2?)(\?|$)/)) return;
    reqBodies.set(d.requestId, decodeBody(d.requestBody));
    const filter = browser.webRequest.filterResponseData(d.requestId);
    const chunks = [];
    filter.ondata = (e) => { chunks.push(e.data); filter.write(e.data); };
    filter.onstop = () => {
      filter.close();
      const buf = new Uint8Array(chunks.reduce((n, c) => n + c.byteLength, 0));
      let off = 0;
      for (const c of chunks) { buf.set(new Uint8Array(c), off); off += c.byteLength; }
      const body = new TextDecoder().decode(buf);
      if (!/^\s*[{[]/.test(body) || body.length > 2_000_000) { reqBodies.delete(d.requestId); return; }
      post("/ext/capture", { url: d.url, method: d.method, requestBody: reqBodies.get(d.requestId),
                             requestHeaders: reqHeaders.get(d.requestId), body }).catch(() => {});
      reqBodies.delete(d.requestId);
      reqHeaders.delete(d.requestId);
    };
    filter.onerror = () => { reqBodies.delete(d.requestId); reqHeaders.delete(d.requestId); };
  },
  { urls: ["https://*.superbuy.com/*"], types: ["xmlhttprequest"] },
  ["blocking", "requestBody"]
);

pollLoop();
