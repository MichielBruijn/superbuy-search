"""Lokale bridge: geeft fetch-opdrachten door aan de Firefox-extensie,
bewaart passieve captures en sessies, en serveert de resultatenpagina."""

import asyncio
import json
import re
import time
import uuid
from collections import deque
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import DATA_DIR, HOST, PORT

STATIC = Path(__file__).parent / "static"
SESSIONS = DATA_DIR / "sessions"
CAPTURES_FILE = DATA_DIR / "captures.jsonl"
SUPERBUY_URL = re.compile(r"^https://([a-z0-9-]+\.)*superbuy\.com/")
MIN_INTERVAL = 1.2  # seconden tussen opdrachten, niet hameren op Superbuy

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
    })


async def api_fetch(req: Request):
    """Body: {url, method?, body?, headers?, timeout?} -> {ok, status, body | error}."""
    global last_fetch
    job = await req.json()
    if not SUPERBUY_URL.match(job.get("url", "")):
        return JSONResponse({"ok": False, "error": "alleen https://*.superbuy.com"}, status_code=400)
    if not connected():
        return JSONResponse({"ok": False, "error": "Firefox-extensie niet verbonden"}, status_code=503)
    timeout = float(job.pop("timeout", 60))
    job["id"] = uuid.uuid4().hex
    job["type"] = "fetch"
    async with fetch_lock:
        wait = MIN_INTERVAL - (time.time() - last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        fut = asyncio.get_running_loop().create_future()
        pending[job["id"]] = fut
        await jobs.put(job)
        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            pending.pop(job["id"], None)
            return JSONResponse({"ok": False, "error": "timeout: geen antwoord van extensie"}, status_code=504)
        finally:
            last_fetch = time.time()
    return JSONResponse(res)


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
        Route("/api/captures", api_captures),
        Route("/api/sessions", api_sessions, methods=["GET", "POST"]),
        Route("/api/sessions/{sid}", api_session, methods=["GET", "DELETE"]),
    ],
    # alleen de extensie (moz-extension://) mag cross-origin praten
    middleware=[Middleware(CORSMiddleware, allow_origin_regex=r"moz-extension://.*",
                           allow_methods=["GET", "POST"], allow_headers=["content-type", "x-superbuy-bridge"]),
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
