"""Lokale bridge: geeft fetch-opdrachten door aan de Firefox-extensie,
bewaart passieve captures en sessies, en serveert de resultatenpagina."""

import asyncio
import json
import os
import random
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import DATA_DIR, HOST, PORT
from .core import PLATFORMS, SearchError, parse_search, search_job

STATIC = Path(__file__).parent / "static"
SESSIONS = DATA_DIR / "sessions"
CAPTURES_FILE = DATA_DIR / "captures.jsonl"
SUPERBUY_URL = re.compile(r"^https://([a-z0-9-]+\.)*superbuy\.com/")
MIN_INTERVAL = 1.2  # seconden tussen opdrachten, niet hameren op Superbuy
CRAWLER_INTERVAL = (4.0, 7.0)  # zoek-/detailcalls: willekeurig, Superbuy rate-limit op /crawler
RATE_LIMITED = 20231101  # "Request too often"; daarna toont de site een hCaptcha
cooldown = {"until": 0.0, "strikes": 0}

jobs: asyncio.Queue = asyncio.Queue()
pending: dict[str, asyncio.Future] = {}
captures: deque = deque(maxlen=300)
ext = {"last_poll": 0.0, "tabs": 0, "version": None}
fetch_lock = asyncio.Lock()
last_fetch = 0.0


# --- extensie-kant ---

async def ext_poll(req: Request):
    ext["last_poll"] = time.time()
    ext["tabs"] = int(req.query_params.get("tabs", 0))
    ext["version"] = req.query_params.get("v")
    try:
        job = await asyncio.wait_for(jobs.get(), timeout=25)
    except TimeoutError:
        return Response(status_code=204)
    ext["last_poll"] = time.time()
    return JSONResponse(job)


async def ext_result(req: Request):
    res = await req.json()
    fut = pending.pop(res.get("id"), None)
    if fut and not fut.done():
        fut.set_result(res)
    return JSONResponse({"ok": True})


async def ext_capture(req: Request):
    cap = await req.json()
    cap["t"] = time.time()
    captures.append(cap)
    # ook als jouw eigen klikken op Superbuy de limiet raken: pauze aanhouden
    if "/crawler" in cap.get("url", "") and f'"state":{RATE_LIMITED}' in cap.get("body", ""):
        cooldown["until"] = max(cooldown["until"], time.time() + 120)
    with CAPTURES_FILE.open("a") as f:
        f.write(json.dumps(cap, ensure_ascii=False) + "\n")
    return JSONResponse({"ok": True})


# --- API voor MCP-server ---

def connected() -> bool:
    return time.time() - ext["last_poll"] < 35


async def api_status(req: Request):
    return JSONResponse({
        "extension_connected": connected(),
        "seconds_since_poll": round(time.time() - ext["last_poll"], 1) if ext["last_poll"] else None,
        "superbuy_tabs": ext["tabs"],
        "extension_version": ext["version"],
        "queued": jobs.qsize(),
        "rate_limit_pause": max(0, int(cooldown["until"] - time.time())),
    })


async def ext_fetch(job: dict, timeout: float = 60) -> dict:
    """Laat de extensie een fetch doen in de Superbuy-tab -> {ok, status, body | error}."""
    global last_fetch
    if not SUPERBUY_URL.match(job.get("url", "")):
        return {"ok": False, "error": "alleen https://*.superbuy.com"}
    if not connected():
        return {"ok": False, "error": "Firefox-extensie niet verbonden"}
    crawler = "/crawler" in job["url"]
    if crawler and time.time() < cooldown["until"]:
        return {"ok": False, "rate_limited": True,
                "error": f"Superbuy rate-limit: pauze nog {int(cooldown['until'] - time.time())}s"}
    job = {**job, "id": uuid.uuid4().hex, "type": "fetch"}
    async with fetch_lock:
        interval = random.uniform(*CRAWLER_INTERVAL) if crawler else MIN_INTERVAL
        wait = interval - (time.time() - last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        fut = asyncio.get_running_loop().create_future()
        pending[job["id"]] = fut
        await jobs.put(job)
        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
            if crawler and f'"state":{RATE_LIMITED}' in (res.get("body") or ""):
                cooldown["strikes"] += 1
                cooldown["until"] = time.time() + min(120 * 2 ** (cooldown["strikes"] - 1), 1800)
                res = {**res, "ok": False, "rate_limited": True,
                       "error": f"Superbuy rate-limit (Request too often); pauze {int(cooldown['until'] - time.time())}s"}
            elif crawler and res.get("ok"):
                cooldown["strikes"] = 0
            return res
        except TimeoutError:
            pending.pop(job["id"], None)
            return {"ok": False, "error": "timeout: geen antwoord van extensie"}
        finally:
            last_fetch = time.time()


async def api_fetch(req: Request):
    """Body: {url, method?, body?, headers?, timeout?}."""
    job = await req.json()
    return JSONResponse(await ext_fetch(job, float(job.pop("timeout", 60))))


_rate = {"v": None, "t": 0.0}


async def eur_per_cny() -> float | None:
    if time.time() - _rate["t"] > 6 * 3600:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get("https://api.frankfurter.app/latest", params={"from": "CNY", "to": "EUR"})
                _rate.update(v=r.json()["rates"]["EUR"], t=time.time())
        except Exception:
            pass
    return _rate["v"]


async def api_search(req: Request):
    """Direct zoeken vanaf de webpagina. Body: {keyword, platforms: [...], page?}."""
    q = await req.json()
    out = {}
    for platform in q.get("platforms") or ["1688"]:
        try:
            items, meta = parse_search(await ext_fetch(search_job(q["keyword"], platform, int(q.get("page", 1)))), platform)
            out[platform] = {"items": items, **meta}
        except SearchError as e:
            out[platform] = {"error": str(e), "items": []}
    out["_eur_per_cny"] = await eur_per_cny()
    return JSONResponse(out)


# --- "Vraag Claude": headless claude -p met alleen de superbuy-tools ---

CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
CLAUDE_MODEL = os.environ.get("SUPERBUY_CLAUDE_MODEL")
PROJECT = Path(__file__).resolve().parents[2]
asks: dict[str, dict] = {}
ask_lock = asyncio.Lock()

ASK_PROMPT = """Je bent een zoekhulp voor Superbuy (Chinese webshops via een agent). Wens van de gebruiker:

«{wish}»
{refine}
Werkwijze:
1. Bedenk 3-6 Chinese zoektermen zoals een Chinese verkoper ze in een titel zet (vakjargon,
   synoniemen, modelnummers, materiaal). Merknamen/modelnummers mogen in het Engels.
2. Zoek ALLEEN op deze platforms (keuze van de gebruiker): {platforms}.
   '1688' (groothandel) en 'xianyu' (tweedehands) hebben de beste relevantie;
   'tb' (Taobao) is een affiliate-zoekmachine met veel ruis maar breed aanbod.
   Doe in totaal hooguit {max_searches} zoekopdrachten; elke zoekopdracht telt mee voor Superbuy's
   rate-limit, dus kies ze met zorg. Meldt search een rate-limit: stop met zoeken en publiceer
   met wat je hebt (vermeld het in de summary).
3. Beoordeel op de Chinese titel, niet de Google-vertaling. Let op lokprijzen (prijs van de
   goedkoopste variant of een accessoire), minimumafname bij 1688, en dat Xianyu tweedehands is.
   Gebruik details(id) voor hooguit 3 topkandidaten waar varianten/prijs of verzending onduidelijk
   zijn; ook dat telt voor de rate-limit.
4. Roep aan het eind precies één keer publish aan met session_id="{sid}": 8-25 items met
   verdict top/goed/misschien/nee ('nee' alleen als het iets leert), score 0-10, title_nl (korte
   Nederlandse titel), notes (1-2 zinnen Nederlands: waarom, aandachtspunten). title: korte titel.
   Noem bedragen in euro's (de gebruiker denkt in €), eventueel met ¥ erachter.
   summary: conclusie plus tips om verder te zoeken. queries: de gebruikte zoektermen.
   request: de wens van de gebruiker.
Antwoord daarna met één zin."""

REFINE = """
Dit is een verfijning van een eerdere zoektocht (zelfde sessie, werk die bij; houd goede items
die nog passen). Eerdere wens: «{request}». Eerdere conclusie: {summary}
Eerdere zoektermen: {queries}
Eerdere items: {items}
"""


def mcp_config(platforms: list[str]) -> str:
    return json.dumps({"mcpServers": {"superbuy": {
        "env": {"SUPERBUY_PLATFORMS": ",".join(platforms)},
        "command": shutil.which("uv") or str(Path.home() / ".local/bin/uv"),
        "args": ["run", "--project", str(PROJECT), "superbuy-mcp"]}}})


def describe(ev: dict) -> list[str]:
    """stream-json-event -> regels voor het voortgangslog."""
    out = []
    if ev.get("type") == "assistant":
        for c in ev["message"].get("content", []):
            if c.get("type") == "tool_use":
                i = c.get("input", {})
                name = c["name"].removeprefix("mcp__superbuy__")
                if name == "search":
                    out.append(f"🔍 {i.get('platform', 'tb')}: {i.get('keyword')}")
                elif name == "details":
                    out.append(f"🔎 details {i.get('id')}")
                elif name == "publish":
                    out.append(f"📋 publiceren: {len(i.get('items', []))} items")
                else:
                    out.append(f"⚙ {name}")
            elif c.get("type") == "text" and c["text"].strip():
                out.append("💬 " + c["text"].strip())
    elif ev.get("type") == "user":
        for c in ev["message"].get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                first = str(body or "").strip().splitlines()[:1]
                if first and not first[0].startswith("Gepubliceerd"):
                    out.append("   → " + first[0][:200])
    elif ev.get("type") == "result" and ev.get("is_error"):
        out.append("❌ " + str(ev.get("result"))[:500])
    return out


async def run_ask(aid: str):
    a = asks[aid]
    async with ask_lock:  # één tegelijk; de extensie doet toch alles na elkaar
        a["status"] = "bezig"
        a["log"].append("Claude gestart…")
        cmd = [CLAUDE, "-p", a["prompt"], "--output-format", "stream-json", "--verbose",
               "--strict-mcp-config", "--mcp-config", mcp_config(a["platforms"]), "--tools", "",
               "--allowedTools", "mcp__superbuy__search", "mcp__superbuy__details", "mcp__superbuy__publish",
               "mcp__superbuy__status",
               "--no-session-persistence"]
        if CLAUDE_MODEL:
            cmd += ["--model", CLAUDE_MODEL]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=DATA_DIR, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
            a["proc"] = proc
            async for line in proc.stdout:
                try:
                    a["log"] += describe(json.loads(line))
                except json.JSONDecodeError:
                    pass
            err = (await proc.stderr.read()).decode()[-500:]
            await proc.wait()
        except OSError as e:
            a["log"].append(f"❌ kon claude niet starten: {e}")
            err = ""
        a.pop("proc", None)
        if session_path(a["sid"]).exists() and session_path(a["sid"]).stat().st_mtime > a["started"]:
            a["status"] = "klaar"
        else:
            a["status"] = "fout"
            if err:
                a["log"].append("❌ " + err)


async def api_ask(req: Request):
    """Body: {wish, refine?: session_id, max_searches?}."""
    q = await req.json()
    wish = (q.get("wish") or "").strip()
    if not wish:
        return JSONResponse({"error": "lege vraag"}, status_code=400)
    refine = ""
    sid = q.get("refine")
    if sid:
        prev = json.loads(session_path(sid).read_text())
        refine = REFINE.format(
            request=prev.get("request"), summary=prev.get("summary"), queries=", ".join(prev.get("queries", [])),
            items="; ".join(f"{i['id']} [{i.get('verdict')}] {i.get('title_nl')}" for i in prev.get("items", [])))
    else:
        sid = time.strftime("%Y%m%d-%H%M%S")
    platforms = [p for p in q.get("platforms") or list(PLATFORMS) if p in PLATFORMS]
    if not platforms:
        return JSONResponse({"error": "geen geldig platform"}, status_code=400)
    aid = uuid.uuid4().hex[:12]
    asks[aid] = {"id": aid, "sid": sid, "wish": wish, "status": "wachtrij", "log": [], "started": time.time(),
                 "platforms": platforms,
                 "prompt": ASK_PROMPT.format(wish=wish, refine=refine, sid=sid, platforms=", ".join(platforms),
                                             max_searches=int(q.get("max_searches", 8)))}
    asyncio.create_task(run_ask(aid))
    return JSONResponse({"id": aid, "sid": sid})


async def api_ask_status(req: Request):
    a = asks.get(req.path_params["aid"])
    if not a:
        return JSONResponse({"error": "onbekend"}, status_code=404)
    if req.method == "DELETE" and a.get("proc"):
        a["proc"].terminate()
        a["log"].append("⏹ gestopt")
    return JSONResponse({k: a[k] for k in ("id", "sid", "wish", "status", "log", "started")})


async def api_asks(req: Request):
    return JSONResponse([{k: a[k] for k in ("id", "sid", "wish", "status", "started")}
                         for a in sorted(asks.values(), key=lambda a: -a["started"])][:10])


async def api_captures(req: Request):
    grep = req.query_params.get("grep")
    limit = int(req.query_params.get("limit", 20))
    rx = re.compile(grep, re.I) if grep else None
    out = [c for c in captures if not rx or rx.search(c.get("url", ""))]
    return JSONResponse(out[-limit:])


# --- sessies (resultaten die ik cureer) ---

SAFE_ID = re.compile(r"^[a-z0-9-]{1,64}$")


def session_path(sid: str) -> Path:
    if not SAFE_ID.match(sid):
        raise ValueError("ongeldige sessie-id")
    return SESSIONS / f"{sid}.json"


async def api_sessions(req: Request):
    if req.method == "POST":
        s = await req.json()
        sid = s.get("id") or time.strftime("%Y%m%d-%H%M%S")
        s["id"] = sid
        s["updated"] = time.time()
        session_path(sid).write_text(json.dumps(s, ensure_ascii=False, indent=1))
        return JSONResponse({"id": sid, "url": f"http://{HOST}:{PORT}/#/s/{sid}"})
    out = []
    for p in sorted(SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        s = json.loads(p.read_text())
        out.append({"id": s["id"], "title": s.get("title"), "updated": s.get("updated"),
                    "count": len(s.get("items", []))})
    return JSONResponse(out)


async def api_session(req: Request):
    try:
        p = session_path(req.path_params["sid"])
    except ValueError:
        return JSONResponse({"error": "ongeldig"}, status_code=400)
    if not p.exists():
        return JSONResponse({"error": "niet gevonden"}, status_code=404)
    if req.method == "DELETE":
        p.unlink()
        return JSONResponse({"ok": True})
    return Response(p.read_text(), media_type="application/json")


async def require_header(req: Request, call_next):
    # custom header dwingt een CORS-preflight af: willekeurige websites kunnen
    # zo geen opdrachten in je Superbuy-sessie laten uitvoeren
    if req.url.path.startswith(("/api/", "/ext/")) and req.method != "OPTIONS" \
            and req.headers.get("x-superbuy-bridge") != "1":
        return JSONResponse({"error": "header ontbreekt"}, status_code=403)
    return await call_next(req)


async def index(req: Request):
    return FileResponse(STATIC / "index.html")


app = Starlette(
    routes=[
        Route("/", index),
        Route("/ext/poll", ext_poll),
        Route("/ext/result", ext_result, methods=["POST"]),
        Route("/ext/capture", ext_capture, methods=["POST"]),
        Route("/api/status", api_status),
        Route("/api/fetch", api_fetch, methods=["POST"]),
        Route("/api/search", api_search, methods=["POST"]),
        Route("/api/ask", api_ask, methods=["POST"]),
        Route("/api/asks", api_asks),
        Route("/api/ask/{aid}", api_ask_status, methods=["GET", "DELETE"]),
        Route("/api/captures", api_captures),
        Route("/api/sessions", api_sessions, methods=["GET", "POST"]),
        Route("/api/sessions/{sid}", api_session, methods=["GET", "DELETE"]),
    ],
    # alleen de extensie (moz-extension://) mag cross-origin praten
    middleware=[Middleware(CORSMiddleware, allow_origin_regex=r"moz-extension://.*",
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["content-type", "x-superbuy-bridge"]),
                Middleware(BaseHTTPMiddleware, dispatch=require_header)],
)


def main():
    SESSIONS.mkdir(parents=True, exist_ok=True)
    if CAPTURES_FILE.exists():
        for line in CAPTURES_FILE.read_text().splitlines()[-300:]:
            try:
                captures.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
