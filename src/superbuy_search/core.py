"""Superbuy-zoeklogica, gedeeld door bridge (direct zoeken) en MCP-server."""

import json
import re
from urllib.parse import quote, urlencode

CRAWLER = "https://front.superbuy.com/crawler/"
# platform -> (endpoint, vaste formuliervelden); afgeleid uit captures van de Superbuy-site
PLATFORMS = {
    "tb": ("search-product", {"platform": "tb", "qtth": 0, "freeShipment": 0, "b2c": 0, "currency": "CNY"}),
    "1688": ("productpage", {"platform": "alibaba", "platformThird": 9}),
    "xianyu": ("productpage", {"platform": "xy", "platformThird": 11}),
}
CJK = re.compile(r"[一-鿿]")
FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"


class SearchError(Exception):
    pass


def superbuy_link(goods_url: str) -> str:
    return "https://www.superbuy.com/en/page/buy/?url=" + quote(goods_url, safe="")


def search_job(keyword: str, platform: str, page: int = 1, extra: dict | None = None) -> dict:
    """Fetch-opdracht voor de extensie."""
    if platform not in PLATFORMS:
        raise SearchError(f"Onbekend platform {platform!r}; kies uit {', '.join(PLATFORMS)}")
    endpoint, fixed = PLATFORMS[platform]
    form = {
        "keyword": keyword, "translate": "false" if CJK.search(keyword) else "true",
        "pageNo": page, "pageSize": 30, "queryType": 0, "perPageSize": 30, "toPage": page, **fixed,
    }
    form.update(extra or {})
    return {"url": CRAWLER + endpoint, "method": "POST", "body": urlencode(form),
            "headers": {"captchaSupport": "hcaptcha", "content-type": FORM_CT}}


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


def parse_search(res: dict, platform: str) -> tuple[list[dict], dict]:
    """Extensie-antwoord -> (items, meta). Gooit SearchError bij fouten."""
    if not res.get("ok"):
        raise SearchError(f"{res.get('error') or res.get('status')} {str(res.get('body', ''))[:300]}")
    d = json.loads(res["body"])
    if d.get("state") != 0:
        raise SearchError(f"Superbuy: {d.get('msg')} {json.dumps(d)[:300]}")
    data = d.get("data") or {}
    datas = data.get("datas") or []
    if datas and "intResults" in datas[0]:  # search-product nest de items
        raw = [r for blk in datas for r in blk.get("intResults") or []]
        pages = (datas[0].get("paginator") or {}).get("pages", "?")
    else:
        raw, pages = datas, data.get("totalPage", "?")
    items = [_norm(r, platform) for r in raw if r.get("goodsUrl")]
    return items, {"pages": pages, "listType": data.get("listType")}


def detail_job(goods_url: str) -> dict:
    return {"url": CRAWLER.rstrip("/"), "method": "POST",
            "body": json.dumps({"userid": 0, "debug": False, "location": 1, "goodUrl": goods_url}),
            "headers": {"captchaSupport": "hcaptcha", "content-type": "application/json"}}


def parse_detail(res: dict, max_skus: int = 40) -> str:
    """Productdetail -> compacte tekst (varianten, verzending, winkel, waarschuwingen)."""
    if not res.get("ok"):
        raise SearchError(f"{res.get('error') or res.get('status')}")
    d = json.loads(res["body"])
    if d.get("state") != 0:
        raise SearchError(f"Superbuy: {d.get('msg')}")
    g = d.get("data") or {}
    shop = g.get("shop") or {}
    p = lambda k: (g.get(k) or {}).get("price")
    lines = [
        f"{g.get('goodsName')} | {g.get('goodsCatPath')}",
        f"Prijs ¥{p('unitPrice')} (actie ¥{p('proPrice')}), verzending binnen China ¥{p('freight')}, "
        f"voorraad {(g.get('repositoryInfo') or {}).get('quantity')}",
        "Winkel: " + str(shop.get("shopName")) + " | " + ", ".join(
            f"{e.get('title')} {str(e.get('score')).strip()}" for e in shop.get("evaluateInfo") or [])
        + (f" | risico: {shop.get('shopRiskText')}" if shop.get("shopRiskText") else ""),
    ]
    try:
        feats = json.loads(g.get("feature") or "[]")
        lines.append("Kenmerken: " + "; ".join(f"{k}: {v}" for f in feats for k, v in f.items()))
    except (json.JSONDecodeError, AttributeError):
        pass
    warn = [w.get("enContent") for w in g.get("itemCusRelList") or [] if w.get("enContent")]
    if g.get("fakeBrand"):
        warn.append("Superbuy markeert dit als mogelijk namaakmerk")
    if g.get("forbiddenBuy"):
        warn.append("Superbuy staat aankoop niet toe")
    if warn:
        lines.append("Waarschuwingen: " + " | ".join(dict.fromkeys(warn)))
    skus = g.get("skuList") or []
    if skus:
        lines.append(f"Varianten ({len(skus)}):")
        for s in skus[:max_skus]:
            name = " / ".join(pr.get("valueName", "") for pr in s.get("props") or [])
            lines.append(f"  ¥{(s.get('price') or {}).get('price')} | voorraad {s.get('quantity')} | {name}")
    return "\n".join(lines)
