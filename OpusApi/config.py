"""Central configuration.

Every setting has a ready default HERE, so the environment / .env only needs the
secrets (Mongo, Telegram, keys). Set an environment variable only to override a default.
See example.env for the list of secrets.
"""
import os

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _s(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name, str(default)))
    except ValueError:
        return int(default)


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return float(default)


def _b(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list:
    raw = os.environ.get(name) or ""
    raw = raw.replace("\n", ",").replace(";", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _channel(value: str) -> str:
    value = value.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value.strip("/")


# ── Public API ───────────────────────────────────────────────
BASE_URL = _s("BASE_URL").rstrip("/")
PORT = _i("PORT", 3000)
CACHE_DIR = _s("CACHE_DIR") or os.path.join(ROOT_DIR, "OpusApi", "saved")
COOKIES_FILE = _s("COOKIES_FILE") or os.path.join(ROOT_DIR, "cookies.txt")
COOKIES_B64 = _s("COOKIES_B64")
COOKIES_URL = _s("COOKIES_URL")  # direct link to a cookies.txt (keep it private!)
COOKIES_REFRESH_HOURS = _f("COOKIES_REFRESH_HOURS", 6.0)  # re-download COOKIES_URL every N hours, 0 = never
ADMIN_KEY = _s("ADMIN_KEY")
RUN_BOT_WITH_API = _b("RUN_BOT_WITH_API", True)
BUTTON_STYLES = _b("BUTTON_STYLES", True)  # coloured inline buttons (Bot API 9.4+)
# Root /download returns the audio file directly (what music bots expect). false = JSON token
LEGACY_DIRECT_DOWNLOAD = _b("LEGACY_DIRECT_DOWNLOAD", True)
ALLOW_VIDEO = _b("ALLOW_VIDEO", False)   # off by default: video needs ffmpeg merge + lots of disk (1 GB hosts)
# Cached songs are sent straight from the Telegram channel to the user (no disk download first)
TELEGRAM_STREAM = _b("TELEGRAM_STREAM", True)

# ── Security ─────────────────────────────────────────────────
KEY_ENC_SECRET = _s("KEY_ENC_SECRET")   # encrypts API keys stored in MongoDB
TOKEN_SECRET = _s("TOKEN_SECRET")       # signs short-lived download tokens
TOKEN_TTL = _i("TOKEN_TTL", 300)
# Owner master key: unlimited quota, no daily limit, no per-minute limit. Keep it SECRET.
_master_raw = _s("MASTER_KEY")
MASTER_KEY_TOO_SHORT = bool(_master_raw) and len(_master_raw) < 24   # too weak -> ignored
MASTER_KEY = "" if MASTER_KEY_TOO_SHORT else _master_raw

# ── Limits / plans ───────────────────────────────────────────
DAILY_LIMIT = _i("DAILY_LIMIT", 150)
KEY_VALID_DAYS = _i("KEY_VALID_DAYS", 28)
RESET_TZ = _s("RESET_TZ", "Asia/Kolkata")
PLAN_NAME = _s("PLAN_NAME", "Free")
RATE_PER_MIN = _i("RATE_PER_MIN", 10)   # per key, on token generation. 0 = off

# ── Engine (protects the free server) ────────────────────────
MIN_FREE_BYTES = _i("MIN_FREE_MB", 200) * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = max(1, _i("MAX_CONCURRENT_DOWNLOADS", 2))
MAX_QUEUE = _i("MAX_QUEUE", 40)
QUEUE_WAIT_SECONDS = _i("QUEUE_WAIT_SECONDS", 60)
DOWNLOAD_TIMEOUT = _i("DOWNLOAD_TIMEOUT", 120)
MAX_CONCURRENT_CACHE_READS = max(1, _i("MAX_CONCURRENT_CACHE_READS", 10))
# Progressive audio: yt-dlp writes the raw m4a to a pipe and clients start receiving bytes
# while the download is still running (no ffmpeg re-encode). false = old behaviour (mp3).
PROGRESSIVE_AUDIO = _b("PROGRESSIVE_AUDIO", True)
LIVE_STALL_SECONDS = _i("LIVE_STALL_SECONDS", 30)   # no new bytes for this long -> abort the download
# yt-dlp YouTube player clients, tried in this order when YouTube refuses extraction
# ("Failed to extract any player response", bot check...). "|" separates the attempts.
# Example for the fastest start (no JS challenge): YTDLP_CLIENTS=android_vr|tv,mweb
CLIENT_SETS = [c.strip() for c in _s("YTDLP_CLIENTS", "tv,mweb|android_vr").split("|") if c.strip()] or ["tv,mweb"]
PROXIES_RAW = _list("PROXIES")
MAX_PROXY_TRIES = max(1, _i("MAX_PROXY_TRIES", 3))

# ── MongoDB (users, keys, usage) ─────────────────────────────
MONGO_URI = _s("MONGO_URI")
MONGO_DB = _s("MONGO_DB", "opusapi")

# ── Telegram audio archive ───────────────────────────────────
TELEGRAM_SESSION = _s("TELEGRAM_SESSION")
TELEGRAM_CACHE_CHANNEL = _s("TELEGRAM_CACHE_CHANNEL")
TELEGRAM_MAX_SONG_MB = _i("TELEGRAM_MAX_SONG_MB", 40)

# ── Telegram key-manager bot ─────────────────────────────────
BOT_TOKEN = _s("BOT_TOKEN")
API_ID = _i("API_ID", 0)
API_HASH = _s("API_HASH")
OWNER_IDS = [int(x) for x in _list("OWNER_IDS") if x.lstrip("-").isdigit()]
FORCE_CHANNELS = [c for c in (_channel(x) for x in _list("FORCE_CHANNELS")) if c]
SUPPORT_URL = _s("SUPPORT_URL")
UPDATES_URL = _s("UPDATES_URL")
OWNER_URL = _s("OWNER_URL")
DEVELOPER_URL = _s("DEVELOPER_URL")
REPORT_URL = _s("REPORT_URL") or SUPPORT_URL
START_MEDIA = _s("START_MEDIA")                    # image / gif / video: https URL or file_id
START_MEDIA_TYPE = _s("START_MEDIA_TYPE", "auto").lower()  # auto | photo | video | gif | none
STRINGS_FILE = _s("STRINGS_FILE")
BROADCAST_DELAY = _f("BROADCAST_DELAY", 0.05)
REMINDER_DAYS = _i("REMINDER_DAYS", 3)             # 0 = no expiry reminders
TEST_MAX_MINUTES = _i("TEST_MAX_MINUTES", 10)      # /test refuses songs longer than this
