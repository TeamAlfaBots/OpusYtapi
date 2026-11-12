"""OpusApi - public YouTube audio API.

Flow:  GET /v1/download (X-API-Key)  ->  signed token
       GET /v1/stream/{id}?token=... ->  audio file
Legacy paths without /v1 still work for existing bots.
"""
import asyncio
import hmac
import logging
import os
import time
import re
from collections import deque
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTasks
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, downloader, security, utils
from .database import mongo, telegram_cache

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("opusapi")

START_TIME = time.time()
MASTER_ID = 0   # pseudo user id used in tokens made with the owner's MASTER_KEY
_bot = {"client": None, "task": None}
_bg = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.MASTER_KEY_TOO_SHORT:
        log.warning("MASTER_KEY is shorter than 24 characters and was IGNORED. Use a long random value.")
    elif config.MASTER_KEY:
        log.info("Master key enabled (unlimited owner access)")
    await downloader.load_cookies()
    _cookie_task = asyncio.create_task(downloader.cookies_refresh_loop())
    downloader.startup_cleanup()
    await mongo.init_mongo()          # non-fatal: auth answers 503 until Mongo is back
    await telegram_cache.start()  # non-fatal: API still works when Telegram cache is unavailable

    if config.RUN_BOT_WITH_API:       # optional: run the key-manager bot in this process
        try:
            from OpusBot.app import build_bot, start_bot

            _bot["client"] = build_bot()
            _bot["task"] = await start_bot(_bot["client"])
            log.info("Key-manager bot started inside the API process")
        except Exception as e:
            log.error("Could not start the bot: %s", e)
    yield
    _cookie_task.cancel()
    await telegram_cache.stop()
    if _bot["client"] is not None:
        try:
            from OpusBot.app import stop_bot

            await stop_bot(_bot["client"], _bot["task"])
        except Exception:
            pass


app = FastAPI(title="OpusApi", lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    body = exc.detail if isinstance(exc.detail, dict) else {
        "status": "error", "code": "http_error", "message": str(exc.detail)
    }
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def _err(status: int, code: str, message: str, headers=None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"status": "error", "code": code, "message": message},
        headers=headers,
    )


# ── per-key rate limit (burst protection) ────────────────────
_hits = {}


def _rate_check(tg_id: int):
    if config.RATE_PER_MIN <= 0:
        return
    now = time.monotonic()
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            del _hits[k]
    dq = _hits.setdefault(tg_id, deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= config.RATE_PER_MIN:
        wait = int(60 - (now - dq[0])) + 1
        raise _err(429, "rate_limited", f"Too many requests. Retry in {wait}s.", {"Retry-After": str(wait)})
    dq.append(now)


def _is_master(key) -> bool:
    """True when `key` is the owner's MASTER_KEY (constant-time compare)."""
    return bool(config.MASTER_KEY and key and
                hmac.compare_digest(key.encode(), config.MASTER_KEY.encode()))


async def _auth(key) -> int:
    """API key -> telegram id (MASTER_ID for the master key). Raises the proper API error."""
    if not key:
        raise _err(401, "missing_key", "Send your API key in the X-API-Key header.")
    if _is_master(key):
        return MASTER_ID
    try:
        state, tg_id = await mongo.lookup_key(key)
    except Exception as e:
        log.error("mongo error: %s", e)
        raise _err(503, "auth_unavailable", "Auth service temporarily unavailable.")
    if state in ("invalid", "revoked"):
        raise _err(401, "invalid_key", "Invalid or revoked API key.")
    if state == "expired":
        raise _err(401, "key_expired", "API key expired. Renew it in the manager bot.")
    _rate_check(tg_id)
    return tg_id


def _refund_bg(tg_id: int):
    if tg_id == MASTER_ID:   # the master key is never charged
        return
    task = asyncio.create_task(mongo.refund(tg_id))
    _bg.add(task)
    task.add_done_callback(_bg.discard)


def _safe_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


class _RedactSecrets(logging.Filter):
    """Never write API keys / download tokens (sent in the URL) to the access log."""

    _RE = re.compile(r"(api_key|token)=[^&\s\"]+", re.I)

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._RE.sub(r"\1=***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactSecrets())

router = APIRouter()


@router.get("/me")
async def me(x_api_key: str | None = Header(default=None), api_key: str | None = None):
    key = x_api_key or api_key
    if not key:
        raise _err(401, "missing_key", "Send your API key in the X-API-Key header.")
    if _is_master(key):
        return {"status": "success", "plan": "master", "key_state": "ok", "expires_at": None,
                "requests_today": 0, "daily_limit": None, "unlimited": True,
                "resets_in_seconds": utils.seconds_until_reset()}
    try:
        state, tg_id = await mongo.lookup_key(key)
        doc = await mongo.get_user(tg_id) if tg_id else None
    except Exception as e:
        log.error("mongo error: %s", e)
        raise _err(503, "auth_unavailable", "Auth service temporarily unavailable.")
    if state in ("invalid", "revoked") or not doc:
        raise _err(401, "invalid_key", "Invalid or revoked API key.")
    return {
        "status": "success",
        "plan": doc.get("plan", config.PLAN_NAME.lower()),
        "key_state": state,
        "expires_at": doc["expires_at"].isoformat() if doc.get("expires_at") else None,
        "requests_today": mongo.view_used(doc),
        "daily_limit": int(doc.get("daily_limit", config.DAILY_LIMIT)),
        "resets_in_seconds": utils.seconds_until_reset(),
    }


@router.get("/download")
async def generate_token(
    url: str,
    type: str = "audio",
    x_api_key: str | None = Header(default=None),
    api_key: str | None = None,  # legacy clients send ?api_key=
):
    tg_id = await _auth(x_api_key or api_key)

    type = (type or "audio").lower()
    if type not in ("audio", "video"):
        raise _err(400, "invalid_type", "type must be 'audio' or 'video'.")
    if type == "video" and not config.ALLOW_VIDEO:
        raise _err(403, "video_disabled", "Video downloads are disabled on this server.")
    video_id = utils.extract_video_id(url)
    if not video_id:
        raise _err(400, "invalid_url", "Send a valid YouTube video id or link.")

    return {
        "status": "success",
        "video_id": video_id,
        "type": type,
        "download_token": security.sign_token(tg_id, video_id, type),
        "expires_in": config.TOKEN_TTL,
        "usage": "Use the token parameter in the /stream endpoint",
    }


@router.get("/search")
async def search_song(
    q: str,
    x_api_key: str | None = Header(default=None),
    api_key: str | None = None,
):
    """Song name -> best YouTube match. Free (not charged), but rate limited per key."""
    await _auth(x_api_key or api_key)
    try:
        found = await downloader.search_video(q)
    except downloader.Busy:
        raise _err(503, "busy", "Server is busy, retry in a few seconds.", {"Retry-After": "10"})
    except downloader.DownloadError as e:
        raise _err(404, "not_found", str(e))
    return {"status": "success", **found}


@router.get("/stream/{video_id}")
async def stream_music(
    request: Request,
    video_id: str,
    token: str | None = None,
    x_download_token: str | None = Header(default=None),
):
    raw = token or x_download_token
    if not raw:
        raise _err(401, "missing_token", "Missing download token.")
    claims, problem = security.verify_token(raw)
    if not claims:
        if problem == "expired":
            raise _err(401, "token_expired", "Token expired. Request a new one.")
        raise _err(401, "invalid_token", "Invalid token.")
    if claims["video_id"] != video_id:
        raise _err(401, "token_mismatch", "Token does not belong to this video.")
    return await _serve(claims["tg_id"], video_id, claims["type"], request)


async def _serve(tg_id: int, video_id: str, type_: str, request: Request | None = None):
    """Charge 1 request, then send the media (disk -> Telegram stream -> fresh download)."""
    if tg_id == MASTER_ID:
        if not config.MASTER_KEY:   # master key was removed: its old tokens die too
            raise _err(401, "invalid_token", "Invalid token.")
        charge = {"state": "ok", "used": 0, "limit": 0, "unlimited": True}
    else:
        # Charge BEFORE doing any heavy work (atomic). Refund if we fail.
        try:
            charge = await mongo.consume(tg_id)
        except Exception as e:
            log.error("mongo error: %s", e)
            raise _err(503, "auth_unavailable", "Auth service temporarily unavailable.")
        if charge["state"] == "invalid":
            raise _err(401, "invalid_key", "API key is invalid or revoked.")
        if charge["state"] == "expired":
            raise _err(401, "key_expired", "API key expired. Renew it in the manager bot.")
        if charge["state"] == "quota":
            wait = utils.seconds_until_reset()
            raise _err(
                429, "daily_limit",
                f"Daily limit of {charge['limit']} requests reached. Resets in {utils.fmt_duration(wait)}.",
                {"Retry-After": str(wait)},
            )

    quota_headers = {
        "X-Requests-Remaining": "unlimited" if charge.get("unlimited") else str(max(0, charge["limit"] - charge["used"])),
        "X-Daily-Limit": "unlimited" if charge.get("unlimited") else str(charge["limit"]),
    }

    try:
        # Fastest path for cached songs that are not on disk: stream them straight from the
        # Telegram channel (no download first). Falls through to the normal path on any problem.
        if type_ == "audio" and not downloader.find_cached_file(video_id):
            tgs = await downloader.stream_from_telegram(
                video_id, request.headers.get("range") if request is not None else None
            )
            if tgs:
                log.info("served %s type=audio source=telegram-stream user=%s", video_id, tg_id)
                headers = {
                    **quota_headers,
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(tgs.end - tgs.start + 1),
                    "X-Source": "telegram-stream",
                }
                if tgs.partial:
                    headers["Content-Range"] = f"bytes {tgs.start}-{tgs.end}/{tgs.size}"
                return StreamingResponse(
                    tgs.body, status_code=206 if tgs.partial else 200,
                    media_type=tgs.media_type, headers=headers,
                )
        media = await downloader.get_media(video_id, type_)
    except downloader.RangeNotSatisfiable as e:
        _refund_bg(tg_id)
        raise _err(416, "range_not_satisfiable", "Requested range is outside the file.",
                   {"Content-Range": f"bytes */{e.size}"})
    except downloader.Busy:
        _refund_bg(tg_id)
        raise _err(503, "busy", "Server is busy, retry in a few seconds.", {"Retry-After": "10"})
    except downloader.DownloadError as e:
        _refund_bg(tg_id)
        raise _err(502, "download_failed", str(e))
    except asyncio.CancelledError:
        _refund_bg(tg_id)
        raise
    except Exception as e:
        _refund_bg(tg_id)
        log.exception("unexpected error for %s: %s", video_id, e)
        raise _err(500, "internal_error", "Internal error.")

    if media.live is not None:
        # Progressive audio: bytes go out while yt-dlp is still downloading (no Content-Length).
        log.info("served %s type=%s source=%s user=%s", video_id, type_, media.source, tg_id)
        return StreamingResponse(
            downloader.follow_live(media.live),
            media_type=media.media_type,
            headers={**quota_headers, "X-Source": media.source, "Cache-Control": "no-store"},
        )

    tasks = BackgroundTasks()
    if media.delete_after:
        tasks.add_task(_safe_remove, media.path)
    if media.persist:
        tasks.add_task(downloader.persist_to_telegram, video_id, media.path)
    log.info("served %s type=%s source=%s user=%s", video_id, type_, media.source, tg_id)
    return FileResponse(
        media.path,
        media_type=media.media_type,
        background=tasks,
        headers={**quota_headers, "X-Source": media.source},
    )


app.include_router(router, prefix="/v1")
@app.get("/download", include_in_schema=False)
async def legacy_download(
    request: Request,
    url: str,
    type: str = "audio",
    x_api_key: str | None = Header(default=None),
    api_key: str | None = None,
):
    """Single-step endpoint for music bots: GET /download?url=&type=&api_key= -> the audio file itself.

    (The two-step token flow stays available under /v1/download + /v1/stream.)"""
    if not config.LEGACY_DIRECT_DOWNLOAD:
        return await generate_token(url=url, type=type, x_api_key=x_api_key, api_key=api_key)
    info = await generate_token(url=url, type=type, x_api_key=x_api_key, api_key=api_key)
    claims, _ = security.verify_token(info["download_token"])
    return await _serve(claims["tg_id"], claims["video_id"], claims["type"], request)


app.include_router(router, include_in_schema=False)  # legacy: /download, /stream


@app.get("/")
async def home():
    return {
        "status": "Running...",
        "service": "OpusApi",
        "uptime": f"{round(time.time() - START_TIME, 2)}s",
        "docs": "Use /v1/download with the X-API-Key header.",
    }


@app.get("/ping")
async def ping():
    """Near-zero-cost endpoint for uptime pingers (keeps Render free awake)."""
    return {"status": "ok"}


@app.get("/stats")
async def stats(x_admin_key: str | None = Header(default=None)):
    if not config.ADMIN_KEY:
        raise _err(404, "not_found", "Not found.")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, config.ADMIN_KEY):
        raise _err(401, "unauthorized", "Bad admin key.")
    try:
        totals = await mongo.get_totals()
    except Exception:
        totals = "unavailable"
    return {
        "status": "success",
        "totals": totals,
        "engine": {**downloader.queue_state(), **downloader.COUNTERS},
        "disk": downloader.disk_stats(),
        "proxies_healthy": f"{downloader.healthy_proxy_count()}/{len(downloader.PROXIES)}",
        "telegram_cache": await telegram_cache.get_cache_stats(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
