"""MCP-server: laat Claude zoeken op Superbuy via de bridge en resultaten publiceren."""

import json
import re
from urllib.parse import quote, urlencode

import httpx
from mcp.server import MCPServer

from . import BRIDGE_URL, DATA_DIR

HDR = {"x-superbuy-bridge": "1"}
CRAWLER = "https://front.superbuy.com/crawler/"
# platform -> (endpoint, vaste formuliervelden); afgeleid uit captures van de Superbuy-site
PLATFORMS = {
    "tb": ("search-product", {"platform": "tb", "qtth": 0, "freeShipment": 0, "b2c": 0, "currency": "CNY"}),
    "1688": ("productpage", {"platform": "alibaba", "platformThird": 9}),
    "xianyu": ("productpage", {"platform": "xy", "platformThird": 11}),
}
ITEMS_FILE = DATA_DIR / "items.json"
CJK = re.compile(r"[一-鿿]")

mcp = MCPServer(
    "superbuy",
    instructions=(
        "Zoeken op Superbuy (Taobao/1688/Xianyu/Weidian) via de eigen Firefox-sessie van de gebruiker. "
        "Werkwijze: vertaal de wens naar meerdere Chinese zoektermen (Taobao-jargon, synoniemen, "
        "merk/model, materiaal), zoek per platform, beoordeel op de Chinese titel (niet de Google-"
        "vertaling), en publiceer een selectie met publish(). Bij een Cloudflare-fout: vraag de "
        "gebruiker de check in Firefox op te lossen. Onbekende API's: superbuy_captures() toont wat "
        "de extensie passief zag terwijl de gebruiker zelf op Superbuy klikte."
    ),
)

_items: dict[str, dict] = {}
_eur_per_cny: float | None = None


def _load_items():
    global _items
    if not _items and ITEMS_FILE.exists():
        _items = json.loads(ITEMS_FILE.read_text())


def _save_items():
    if len(_items) > 5000:
        for k in list(_items)[:-5000]:
            del _items[k]
    ITEMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_FILE.write_text(json.dumps(_items, ensure_ascii=False))


async def _bridge(method: str, path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BRIDGE_URL, headers=HDR, timeout=90) as c:
        return await c.request(method, path, **kw)


async def _sb_fetch(url: str, method: str = "GET", body: str | None = None,
                    content_type: str | None = None) -> dict:
    job = {"url": url, "method": method, "body": body, "headers": {"captchaSupport": "hcaptcha"}}
    if content_type:
        job["headers"]["content-type"] = content_type
    try:
        r = await _bridge("POST", "/api/fetch", json=job)
    except httpx.ConnectError:
        return {"ok": False, "error": "bridge draait niet (systemctl --user start superbuy-bridge)"}
    return r.json()


def superbuy_link(goods_url: str) -> str:
    return "https://www.superbuy.com/en/page/buy/?url=" + quote(goods_url, safe="")


def _norm(r: dict, platform: str) -> dict:
    gid = str(r.get("goodsId") or r.get("goodsCode") or "")
    shop = r.get("shop") or {}
    return {
        "id": f"{platform}:{gid}",
        "platform": platform,
        "goodsId": gid,
        "url": r.get("goodsUrl"),
        "superbuy": superbuy_link(r["goodsUrl"]) if r.get("goodsUrl") else None,
        "img": (r.get("imgUrl") or "").replace("http://", "https://", 1) or None,
        "price": r.get("priceTemp") or r.get("price"),  # priceTemp is altijd CNY
        "titleCn": r.get("titleCn"),
        "titleEn": r.get("title"),
        "sales": r.get("statusText"),
        "shop": shop.get("shopName"),
        "platformName": r.get("platformName"),
    }


@mcp.tool()
async def status() -> str:
    """Status van bridge en Firefox-extensie."""
    try:
        r = await _bridge("GET", "/api/status")
    except httpx.ConnectError:
        return "Bridge draait niet. Start: systemctl --user start superbuy-bridge"
    return json.dumps(r.json(), ensure_ascii=False)


@mcp.tool()
async def search(keyword: str, platform: str = "tb", page: int = 1,
                 extra: dict[str, str] | None = None) -> str:
    """Zoek op Superbuy. platform: 'tb' (Taobao/Tmall; affiliate-zoekmachine, veel ruis),
    '1688' (groothandel, betere relevantie) of 'xianyu' (tweedehands).
    Chinese zoektermen werken het best (translate gaat dan uit).
    extra: extra/overschrijvende formuliervelden (sortering, prijsfilter e.d.; zie
    superbuy_captures(grep='crawler') voor wat de site zelf stuurt).
    Geeft compacte regels: id | prijs ¥ | verkopen | winkel | Chinese titel | Google-titel."""
    _load_items()
    if platform not in PLATFORMS:
        return f"Onbekend platform {platform!r}; kies uit {', '.join(PLATFORMS)}"
    endpoint, fixed = PLATFORMS[platform]
    form = {
        "keyword": keyword, "translate": "false" if CJK.search(keyword) else "true",
        "pageNo": page, "pageSize": 30, "queryType": 0, "perPageSize": 30, "toPage": page, **fixed,
    }
    form.update(extra or {})
    res = await _sb_fetch(CRAWLER + endpoint, "POST", urlencode(form),
                          "application/x-www-form-urlencoded; charset=UTF-8")
    if not res.get("ok"):
        return f"Fout: {res.get('error') or res.get('status')} {str(res.get('body', ''))[:300]}"
    d = json.loads(res["body"])
    if d.get("state") != 0:
        return f"Superbuy-fout: {d.get('msg')} {json.dumps(d)[:300]}"
    data = d.get("data") or {}
    datas = data.get("datas") or []
    if datas and "intResults" in datas[0]:  # search-product nest de items
        raw = [r for blk in datas for r in blk.get("intResults") or []]
        pages = (datas[0].get("paginator") or {}).get("pages", "?")
    else:
        raw, pages = datas, data.get("totalPage", "?")
    items = [_norm(r, platform) for r in raw if r.get("goodsUrl")]
    for it in items:
        _items[it["id"]] = it
    _save_items()
    lines = [f"{len(items)} resultaten, pagina {page}/{pages} "
             f"(listType {data.get('listType')})"]
    for it in items:
        lines.append(f"{it['id']} | ¥{it['price']} | {it['sales']} | {it['shop']} | "
                     f"{it['titleCn']} | {it['titleEn']}")
    return "\n".join(lines)


@mcp.tool()
async def superbuy_fetch(url: str, method: str = "GET", body: str | None = None,
                         content_type: str | None = None, max_chars: int = 4000) -> str:
    """Ruwe fetch binnen de Superbuy-tab (alleen *.superbuy.com), voor API-verkenning."""
    res = await _sb_fetch(url, method, body, content_type)
    b = res.pop("body", "") or ""
    return json.dumps(res, ensure_ascii=False) + "\n" + b[:max_chars]


@mcp.tool()
async def superbuy_captures(grep: str | None = None, limit: int = 10, max_chars: int = 1500) -> str:
    """API-verkeer dat de extensie passief zag (url-regex grep). Toont request en begin van response."""
    r = await _bridge("GET", "/api/captures", params={"grep": grep or "", "limit": limit})
    out = []
    for c in r.json():
        out.append(f"### {c['method']} {c['url']}\nheaders: {json.dumps(c.get('requestHeaders'))}\n"
                   f"body: {c.get('requestBody')}\n→ {c['body'][:max_chars]}")
    return "\n\n".join(out) or "Geen captures."


async def _eur_rate() -> float | None:
    global _eur_per_cny
    if _eur_per_cny is None:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get("https://api.frankfurter.app/latest", params={"from": "CNY", "to": "EUR"})
                _eur_per_cny = r.json()["rates"]["EUR"]
        except Exception:
            return None
    return _eur_per_cny


@mcp.tool()
async def publish(title: str, request: str, items: list[dict], summary: str = "",
                  queries: list[str] | None = None, session_id: str | None = None) -> str:
    """Publiceer een gecureerde selectie naar de resultatenpagina.
    items: [{id, title_nl, notes, verdict: 'top'|'goed'|'misschien'|'nee', score?: 0-10}] —
    id zoals uit search(); prijs/foto/links worden aangevuld uit de cache.
    request: de oorspronkelijke wens van de gebruiker. queries: gebruikte zoektermen.
    session_id: meegeven om een bestaande sessie bij te werken."""
    _load_items()
    full, missing = [], []
    for a in items:
        base = _items.get(a["id"])
        if not base:
            missing.append(a["id"])
            continue
        full.append({**base, **a})
    session = {"title": title, "request": request, "summary": summary, "queries": queries or [],
               "eur_per_cny": await _eur_rate(), "items": full}
    if session_id:
        session["id"] = session_id
    r = await _bridge("POST", "/api/sessions", json=session)
    out = r.json()
    msg = f"Gepubliceerd: {out['url']} ({len(full)} items, sessie {out['id']})"
    if missing:
        msg += f"\nOnbekende ids (eerst zoeken): {', '.join(missing)}"
    return msg


def main():
    mcp.run()


if __name__ == "__main__":
    main()
