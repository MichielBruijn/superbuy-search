// Voert fetches uit als de pagina zelf (content.fetch): zelfde cookies,
// origin en Cloudflare-clearance als wanneer jij op Superbuy klikt.
if (!window.__sbBridge) {
  window.__sbBridge = true;
  browser.runtime.onMessage.addListener(async (job) => {
    if (job.type !== "fetch") return;
    try {
      const init = { method: job.method || "GET", credentials: "include", headers: job.headers || {} };
      if (job.body != null) init.body = job.body;
      const r = await content.fetch(job.url, cloneInto(init, window, { cloneFunctions: false }));
      return { id: job.id, ok: r.ok, status: r.status, contentType: r.headers.get("content-type"), body: await r.text() };
    } catch (e) {
      return { id: job.id, ok: false, error: String(e) };
    }
  });
}
