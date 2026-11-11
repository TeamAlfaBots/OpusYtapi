"""Media engine: disk cache -> Telegram cache -> yt-dlp.

Built to survive a small free server:
  * at most MAX_CONCURRENT_DOWNLOADS yt-dlp/ffmpeg jobs run at once,
  * extra requests wait in a bounded queue, otherwise they get "busy" (503),
  * every job has a hard timeout,
  * one download per song at a time (concurrent requests share the result).
"""
import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from . import config, utils
from .database import telegram_cache

log = logging.getLogger("opusapi.engine")
os.makedirs(config.CACHE_DIR, exist_ok=True)

AUDIO_EXTS = ("mp3", "m4a", "opus", "webm", "ogg")
_MEDIA_EXTS = set(AUDIO_EXTS) | {"mp4", "mkv"}
_AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "opus": "audio/ogg", "ogg": "audio/ogg", "webm": "audio/webm"}
_VIDEO_TYPES = {"mp4": "video/mp4", "mkv": "video/x-matroska", "webm": "video/webm"}

# Files currently being uploaded to Telegram: LRU cleanup must not delete them.
_PROTECTED = set()

COUNTERS = {"disk_hits": 0, "telegram_hits": 0, "downloads": 0, "busy": 0, "failed": 0}


class Busy(Exception):
    """Server is at capacity (queue full / waited too long)."""


class DownloadError(Exception):
    """yt-dlp failed. str(e) is safe to show to API users."""


class RangeNotSatisfiable(Exception):
    """Range header starts beyond the end of the file (HTTP 416)."""

    def __init__(self, size: int):
        super().__init__("range not satisfiable")
        self.size = size


@dataclass
class TgStream:
    """A cached song that is streamed straight from Telegram to the user."""
    body: object             # async generator of bytes (already primed with its first chunk)
    media_type: str
    size: int                # full file size
    start: int
    end: int                 # inclusive
    partial: bool            # True -> answer 206


@dataclass
class Media:
    path: str
    media_type: str
    source: str              # disk | telegram | download
    delete_after: bool = False   # video: temp file, remove after sending
    persist: bool = False        # fresh audio: save to Telegram in the background


def media_type_for(ext: str, type_: str) -> str:
    table = _AUDIO_TYPES if type_ == "audio" else _VIDEO_TYPES
    return table.get(ext.lower(), "application/octet-stream")


# ── proxy pool ───────────────────────────────────────────────
PROXIES = [p for p in (utils.parse_proxy(r) for r in config.PROXIES_RAW) if p]
FAIL_THRESHOLD = 3
BLOCK_DURATION = 30 * 60
_proxy_index = 0
_fail_count = {p: 0 for p in PROXIES}
_blocked_until = {p: 0.0 for p in PROXIES}


def mark_proxy_fail(proxy):
    _fail_count[proxy] += 1
    if _fail_count[proxy] >= FAIL_THRESHOLD:
        _blocked_until[proxy] = time.time() + BLOCK_DURATION


def mark_proxy_ok(proxy):
    _fail_count[proxy] = 0
    _blocked_until[proxy] = 0.0


def healthy_proxy_count() -> int:
    now = time.time()
    return sum(1 for p in PROXIES if _blocked_until[p] <= now)


def proxy_order() -> list:
    global _proxy_index
    now = time.time()
    available = [p for p in PROXIES if _blocked_until[p] <= now] or list(PROXIES)
    start = _proxy_index % len(available)
    _proxy_index += 1
    return available[start:] + available[:start]


# ── cookies ──────────────────────────────────────────────────
_RUNTIME_COOKIES = os.path.join(tempfile.gettempdir(), "opus_cookies.txt")


def _write_runtime_cookies(data: bytes) -> None:
    tmp = _RUNTIME_COOKIES + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, _RUNTIME_COOKIES)  # atomic: running downloads keep their own copy


async def _fetch_cookies_url() -> bool:
    """Download cookies from COOKIES_URL. Never logs the URL (it may hold a token)."""
    url = config.COOKIES_URL
    if not url:
        return False
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    log.warning("COOKIES_URL answered HTTP %s", r.status)
                    return False
                data = await r.content.read(2_000_000)
    except Exception as e:
        log.warning("COOKIES_URL download failed: %s", type(e).__name__)
        return False
    head = data.lstrip()[:20].lower()
    if len(data.strip()) <= 10 or head.startswith((b"<!doc", b"<html", b"{")):
        log.warning("COOKIES_URL did not return a cookies.txt file (got a web page or empty file)")
        return False
    _write_runtime_cookies(data)
    log.info("YouTube cookies loaded from COOKIES_URL (%d bytes)", len(data))
    return True


async def load_cookies() -> bool:
    """Startup: COOKIES_URL first, then COOKIES_B64 / cookies file."""
    if config.COOKIES_URL and await _fetch_cookies_url():
        return True
    if config.COOKIES_URL:
        log.warning("Falling back to COOKIES_B64 / cookies file")
    return prepare_cookies()


async def cookies_refresh_loop() -> None:
    """Re-download COOKIES_URL regularly so cookies can be renewed without a redeploy."""
    hours = config.COOKIES_REFRESH_HOURS
    if not config.COOKIES_URL or hours <= 0:
        return
    while True:
        await asyncio.sleep(hours * 3600)
        if not await _fetch_cookies_url():
            log.warning("Cookies refresh failed, keeping the previous cookies")


def _cookie_paths() -> list:
    """Where to look for a cookies file (first existing one wins)."""
    paths = [
        config.COOKIES_FILE,
        "/etc/secrets/cookies.txt",  # Render "Secret Files" (Docker)
        os.path.join(config.ROOT_DIR, "cookies.txt"),
    ]
    seen, out = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def prepare_cookies() -> bool:
    """Copy cookies (COOKIES_B64 or a cookies file) to a writable temp file.
    Platforms like Render mount secret files read-only, but yt-dlp wants to write."""
    data = b""
    if config.COOKIES_B64:
        try:
            data = base64.b64decode(config.COOKIES_B64)
        except Exception:
            log.error("COOKIES_B64 is not valid base64, trying a cookies file instead")
    if len(data.strip()) <= 10:  # no usable B64 -> look for a file
        for path in _cookie_paths():
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError as e:
                    log.warning("Cannot read cookies file %s: %s", path, e)
                    continue
                if len(data.strip()) > 10:
                    log.info("Cookies file found: %s (%d bytes)", path, len(data))
                    break
                log.warning("Cookies file %s is empty", path)
    if len(data.strip()) > 10:
        with open(_RUNTIME_COOKIES, "wb") as f:
            f.write(data)
        log.info("YouTube cookies loaded")
        return True
    if os.path.exists(_RUNTIME_COOKIES):
        os.remove(_RUNTIME_COOKIES)
    log.warning("No YouTube cookies configured (some videos may fail the bot check)")
    return False


def startup_cleanup():
    """Remove half-finished downloads left by a crash/restart."""
    for name in os.listdir(config.CACHE_DIR):
        if ".tmp." in name or name.endswith((".part", ".ytdl")):
            try:
                os.remove(os.path.join(config.CACHE_DIR, name))
            except OSError:
                pass


# ── disk cache + LRU eviction ────────────────────────────────
def find_cached_file(video_id: str):
    for ext in AUDIO_EXTS:
        path = os.path.join(config.CACHE_DIR, f"{video_id}.{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def touch_file(path: str):
    try:
        os.utime(path, None)
    except OSError:
        pass


def _free_bytes() -> int:
    st = os.statvfs(config.CACHE_DIR)
    return st.f_bavail * st.f_frsize


def free_up_space():
    """Delete least-recently-used cached media until MIN_FREE_MB is free."""
    try:
        if _free_bytes() >= config.MIN_FREE_BYTES:
            return
        entries = []
        for name in os.listdir(config.CACHE_DIR):
            if ".tmp." in name or name.rsplit(".", 1)[-1].lower() not in _MEDIA_EXTS:
                continue
            path = os.path.join(config.CACHE_DIR, name)
            if path in _PROTECTED:
                continue
            if os.path.isfile(path):
                entries.append((os.path.getmtime(path), path))
        for _, path in sorted(entries):
            if _free_bytes() >= config.MIN_FREE_BYTES:
                break
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception:
        pass  # cleanup must never block a download


def disk_stats() -> dict:
    size = 0
    for name in os.listdir(config.CACHE_DIR):
        path = os.path.join(config.CACHE_DIR, name)
        if os.path.isfile(path):
            size += os.path.getsize(path)
    return {"cache_mb": round(size / 1048576, 2), "free_mb": round(_free_bytes() / 1048576, 2)}


# ── concurrency control ──────────────────────────────────────
_dl_sem = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
_cache_sem = asyncio.Semaphore(config.MAX_CONCURRENT_CACHE_READS)
_waiting = 0
_running = 0


def queue_state() -> dict:
    return {"running": _running, "waiting": _waiting, "max_concurrent": config.MAX_CONCURRENT_DOWNLOADS}


async def _limited(factory):
    """Run factory() under the global download limit, or raise Busy."""
    global _waiting, _running
    if _waiting >= config.MAX_QUEUE:
        COUNTERS["busy"] += 1
        raise Busy("queue full")
    _waiting += 1
    try:
        await asyncio.wait_for(_dl_sem.acquire(), timeout=config.QUEUE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        COUNTERS["busy"] += 1
        raise Busy("waited too long")
    finally:
        _waiting -= 1
    _running += 1
    try:
        return await factory()
    finally:
        _running -= 1
        _dl_sem.release()


_locks = {}


@asynccontextmanager
async def _song_lock(key: str):
    """One lock per song; removed again when nobody uses it (no leak)."""
    entry = _locks.get(key)
    if entry is None:
        entry = _locks[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _locks.pop(key, None)


# ── yt-dlp ───────────────────────────────────────────────────
def _build_cmd(video_id, type_, outtmpl, cookies, proxy) -> list:
    cmd = [
        "yt-dlp", "--quiet", "--no-warnings", "--no-playlist", "--socket-timeout", "20",
        *cookies,
        "--js-runtimes", "deno",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=tv,mweb",
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    if type_ == "audio":
        # Re-encode to a fixed 128 kbps MP3: small, predictable files (~3-4 MB/song).
        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "128K"]
    else:
        cmd += ["-f", "(bestvideo[height<=?720]+bestaudio)/best"]
    # Strict 11-char id is validated by the caller, and we pass a full URL,
    # so user input can never be parsed as a yt-dlp option.
    cmd += ["-o", outtmpl, f"https://www.youtube.com/watch?v={video_id}"]
    return cmd


async def _run(cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=config.DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        return -9, "timeout"
    except asyncio.CancelledError:  # client went away
        _kill(proc)
        raise
    return proc.returncode, err.decode(errors="ignore")


def _kill(proc):
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _proxy_related(rc, err) -> bool:
    low = err.lower()
    return rc == -9 or any(
        s in low for s in ("429", "too many requests", "blocked", "sign in to confirm", "proxy", "unable to connect")
    )


def _sanitize(err: str) -> str:
    err = re.sub(r"://[^@\s/]+@", "://***@", err)  # never leak proxy credentials
    return err.strip()[-300:]


def _cleanup(prefix: str):
    for name in os.listdir(config.CACHE_DIR):
        if name.startswith(prefix):
            try:
                os.remove(os.path.join(config.CACHE_DIR, name))
            except OSError:
                pass


def _find_tmp(prefix: str):
    for name in os.listdir(config.CACHE_DIR):
        if name.startswith(prefix) and not name.endswith((".part", ".ytdl", ".temp")):
            return os.path.join(config.CACHE_DIR, name)
    return None


async def _download(video_id: str, type_: str, tag: str) -> str:
    prefix = f"{video_id}.{tag}.tmp."
    outtmpl = os.path.join(config.CACHE_DIR, prefix + "%(ext)s")
    cookie_copy = os.path.join(tempfile.gettempdir(), f"opus_ck_{uuid.uuid4().hex[:10]}.txt")
    success = False
    try:
        cookies = []
        if os.path.isfile(_RUNTIME_COOKIES):
            shutil.copyfile(_RUNTIME_COOKIES, cookie_copy)  # private copy per job
            cookies = ["--cookies", cookie_copy]

        tries = proxy_order()[: config.MAX_PROXY_TRIES] if PROXIES else [None]
        last_err, ok = "", False
        for proxy in tries:
            rc, err = await _run(_build_cmd(video_id, type_, outtmpl, cookies, proxy))
            if rc == 0:
                ok = True
                if proxy:
                    mark_proxy_ok(proxy)
                break
            last_err = err
            if proxy and _proxy_related(rc, err):
                mark_proxy_fail(proxy)
                continue
            break  # not proxy related (private video, bad id...): another proxy won't help
        if not ok:
            log.warning("yt-dlp failed for %s: %s", video_id, _sanitize(last_err))
            raise DownloadError("Could not download this video")

        path = _find_tmp(prefix)
        if not path or os.path.getsize(path) == 0:
            raise DownloadError("Download produced no file")
        success = True
        return path
    finally:
        if not success:
            _cleanup(prefix)
        try:
            os.remove(cookie_copy)
        except OSError:
            pass


# ── public entry points ──────────────────────────────────────
async def _from_telegram(video_id: str):
    async with _cache_sem:
        hit = await telegram_cache.get_cached_song(video_id, "audio")
        if not hit:
            return None
        path = hit["path"]
        ext = re.sub(r"[^a-z0-9]", "", (hit.get("ext") or path.rsplit(".", 1)[-1]).lower())[:5] or "mp3"
        final = os.path.join(config.CACHE_DIR, f"{video_id}.{ext}")
        if os.path.abspath(path) != os.path.abspath(final):
            free_up_space()
            os.replace(path, final)
        return Media(final, media_type_for(ext, "audio"), "telegram")


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)


async def get_media(video_id: str, type_: str) -> Media:
    """Return a file to send. Raises Busy / DownloadError."""
    if type_ == "audio":
        path = find_cached_file(video_id)
        if path:
            touch_file(path)
            COUNTERS["disk_hits"] += 1
            return Media(path, media_type_for(path.rsplit(".", 1)[-1], "audio"), "disk")

        async with _song_lock(f"{video_id}:audio"):
            path = find_cached_file(video_id)  # finished while we waited
            if path:
                touch_file(path)
                COUNTERS["disk_hits"] += 1
                return Media(path, media_type_for(path.rsplit(".", 1)[-1], "audio"), "disk")

            if telegram_cache.enabled():
                media = await _from_telegram(video_id)
                if media:
                    COUNTERS["telegram_hits"] += 1
                    return media

            free_up_space()
            try:
                tmp = await _limited(lambda: _download(video_id, "audio", "a"))
            except DownloadError:
                COUNTERS["failed"] += 1
                raise
            ext = tmp.rsplit(".", 1)[-1].lower()
            final = os.path.join(config.CACHE_DIR, f"{video_id}.{ext}")
            os.replace(tmp, final)
            COUNTERS["downloads"] += 1
            return Media(final, media_type_for(ext, "audio"), "download", persist=True)

    # video: never cached, temp file removed after it was sent
    free_up_space()
    try:
        tmp = await _limited(lambda: _download(video_id, "video", "v" + uuid.uuid4().hex[:8]))
    except DownloadError:
        COUNTERS["failed"] += 1
        raise
    COUNTERS["downloads"] += 1
    return Media(tmp, media_type_for(tmp.rsplit(".", 1)[-1], "video"), "download", delete_after=True)


async def stream_from_telegram(video_id: str, range_header=None):
    """Cached song -> TgStream that sends it from the Telegram channel with NO disk copy.

    Returns None when the song is not cached or streaming is not possible (the caller then
    falls back to disk / yt-dlp). Raises RangeNotSatisfiable for a bad Range header.
    """
    if not config.TELEGRAM_STREAM or not telegram_cache.enabled():
        return None
    info = await telegram_cache.open_stream(video_id)
    if not info:
        return None
    size = info["size"]
    try:
        rng = utils.parse_range(range_header, size)
    except ValueError:
        raise RangeNotSatisfiable(size)
    start, end = rng or (0, size - 1)

    it = telegram_cache.iter_range(info["message"], start, end).__aiter__()
    try:  # prime: if Telegram fails now we can still fall back to the normal path
        first = await asyncio.wait_for(it.__anext__(), timeout=30)
    except Exception as e:
        log.warning("Telegram stream failed for %s: %s", video_id, type(e).__name__)
        try:
            await it.aclose()
        except Exception:
            pass
        return None

    async def body():
        acquired = False
        try:
            await _cache_sem.acquire()   # limit simultaneous streams on the one Telegram session
            acquired = True
            yield first
            async for chunk in it:
                yield chunk
        finally:
            if acquired:
                _cache_sem.release()
            try:
                await it.aclose()
            except Exception:
                pass

    COUNTERS["telegram_hits"] += 1
    return TgStream(
        body=body(),
        media_type=media_type_for(info["ext"], "audio"),
        size=size, start=start, end=end, partial=rng is not None,
    )


# ── search (used by the bot's /test and GET /v1/search) ──────
_SEARCH_CACHE = {}
_SEARCH_TTL = 600


def _search_cmd(query, cookies, proxy) -> list:
    cmd = ["yt-dlp", "--quiet", "--no-warnings", "--flat-playlist", "--socket-timeout", "20", *cookies]
    if proxy:
        cmd += ["--proxy", proxy]
    # One argument starting with "ytsearch3:" can never be parsed as an option.
    cmd += ["--print", "%(id)s\t%(title)s\t%(duration)s\t%(uploader)s", "ytsearch3:" + query]
    return cmd


async def _run_out(cmd, timeout):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        return -9, "", "timeout"
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


def _parse_search(out: str):
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not utils.extract_video_id(parts[0]):
            continue
        try:
            duration = int(float(parts[2]))
        except ValueError:
            duration = 0            # live streams / unknown length
        return {"video_id": parts[0], "title": parts[1][:200], "duration": duration,
                "uploader": ("" if parts[3] in ("NA", "None") else parts[3])[:100]}
    return None


async def search_video(query: str) -> dict:
    """Song name -> {video_id, title, duration, uploader}. Raises Busy / DownloadError."""
    query = re.sub(r"\s+", " ", query or "").strip()[:100]
    if len(query) < 2:
        raise DownloadError("Type a song name (at least 2 letters)")
    key = query.lower()
    hit = _SEARCH_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    async def work():
        cookie_copy = os.path.join(tempfile.gettempdir(), f"opus_ck_{uuid.uuid4().hex[:10]}.txt")
        try:
            cookies = []
            if os.path.isfile(_RUNTIME_COOKIES):
                shutil.copyfile(_RUNTIME_COOKIES, cookie_copy)
                cookies = ["--cookies", cookie_copy]
            tries = proxy_order()[: config.MAX_PROXY_TRIES] if PROXIES else [None]
            last = ""
            for proxy in tries:
                rc, out, err = await _run_out(_search_cmd(query, cookies, proxy), min(30, config.DOWNLOAD_TIMEOUT))
                if rc == 0:
                    if proxy:
                        mark_proxy_ok(proxy)
                    found = _parse_search(out)
                    if found:
                        return found
                    raise DownloadError("No result found for this name")
                last = err
                if proxy and _proxy_related(rc, err):
                    mark_proxy_fail(proxy)
                    continue
                break
            log.warning("yt-dlp search failed: %s", _sanitize(last))
            raise DownloadError("Search failed, try again")
        finally:
            try:
                os.remove(cookie_copy)
            except OSError:
                pass

    result = await _limited(work)
    if len(_SEARCH_CACHE) > 500:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[key] = (time.monotonic() + _SEARCH_TTL, result)
    return result


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def persist_to_telegram(video_id: str, path: str):
    """Background job after a fresh audio download. Never raises."""
    if not telegram_cache.enabled():
        return
    try:
        if os.path.getsize(path) > config.TELEGRAM_MAX_SONG_MB * 1048576:
            log.info("not caching %s: file exceeds TELEGRAM_MAX_SONG_MB", video_id)
            return
        ext = path.rsplit(".", 1)[-1].lower()
        _PROTECTED.add(path)
        try:
            if await telegram_cache.save_song(video_id, "audio", ext, path):
                log.info("SAVED TO TELEGRAM CACHE: %s", video_id)
        finally:
            _PROTECTED.discard(path)
    except Exception as e:
        log.warning("Telegram cache save failed for %s: %s", video_id, e)
