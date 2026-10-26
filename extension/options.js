const DEFAULT_BRIDGE = "http://127.0.0.1:8765";
const $ = (id) => document.getElementById(id);

// "http://pc:8765/login?token=abc" of "http://pc:8765" -> {bridge, token}
function parse(text) {
  const u = new URL(text.trim() || DEFAULT_BRIDGE);
  return { bridge: u.origin, token: u.searchParams.get("token") || "" };
}

async function test(cfg) {
  try {
    const r = await fetch(cfg.bridge + "/api/status", {
      headers: { "x-superbuy-bridge": "1", ...(cfg.token ? { "x-superbuy-token": cfg.token } : {}) },
    });
    if (r.status === 401 || r.status === 403) return "❌ bridge weigert: token fout of geen LAN/VPN-adres";
    const s = await r.json();
    return `✅ verbonden met bridge (zoekt nu via ${s.worker_is_you ? "deze browser" : s.worker})`;
  } catch (e) {
    return `❌ bridge onbereikbaar: ${e}`;
  }
}

async function save(cfg) {
  await browser.storage.local.set(cfg);
  $("link").value = cfg.bridge + (cfg.token ? "/login?token=" + cfg.token : "");
  $("msg").textContent = "testen…";
  $("msg").textContent = await test(cfg);
}

$("save").addEventListener("click", () => {
  let cfg;
  try {
    cfg = parse($("link").value);
  } catch {
    $("msg").textContent = "❌ geen geldige URL";
    return;
  }
  // toestemming vragen moet direct in de klik-handler, vóór enige await
  const ask = cfg.bridge === DEFAULT_BRIDGE ? Promise.resolve(true)
    : browser.permissions.request({ origins: [cfg.bridge + "/*"] }).catch(() => true);
  ask.then(() => save(cfg));
});

$("reset").addEventListener("click", () => save({ bridge: DEFAULT_BRIDGE, token: "" }));

browser.storage.local.get(["bridge", "token"]).then((s) => {
  if (s.bridge) $("link").value = s.bridge + (s.token ? "/login?token=" + s.token : "");
});
