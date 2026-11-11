"""Small pure helpers (no network, no framework imports)."""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import config

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_PATTERNS = [
    r"[?&]v=([A-Za-z0-9_-]{11})",
    r"youtu\.be/([A-Za-z0-9_-]{11})",
    r"/shorts/([A-Za-z0-9_-]{11})",
    r"/embed/([A-Za-z0-9_-]{11})",
    r"/live/([A-Za-z0-9_-]{11})",
]


def extract_video_id(value):
    """Return a strict 11-char YouTube id, or None if the input is not one."""
    value = (value or "").strip()
    if _ID_RE.match(value):
        return value
    for pattern in _URL_PATTERNS:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return None


def parse_proxy(raw):
    """'host:port:user:pass' | 'host:port' | full URL  ->  proxy URL (or None)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return None


def parse_range(header, size: int):
    """HTTP Range header -> (start, end) inclusive, or None to send the whole file.

    Only a single range is supported (what players and bots send). A malformed or
    multi-range header is ignored (whole file). Raises ValueError when the range
    starts beyond the end of the file (answer 416).
    """
    if not header or size <= 0:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    a, b = m.group(1), m.group(2)
    if not a:                      # suffix: the last N bytes
        n = int(b)
        if n == 0:
            raise ValueError("unsatisfiable")
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if b and end < start:
        return None                # invalid -> ignore the header
    if start >= size:
        raise ValueError("unsatisfiable")
    return start, min(end, size - 1)


def shard_index(video_id: str, n: int) -> int:
    """Stable shard for a video id. md5 (NOT hash()) so it survives restarts."""
    return int(hashlib.md5(video_id.encode()).hexdigest(), 16) % n


# ── time helpers ─────────────────────────────────────────────
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(config.RESET_TZ)
        except Exception:
            pass
    return timezone.utc


def today_str(now=None) -> str:
    """Date string in RESET_TZ. The daily quota resets when this changes."""
    return (now or now_utc()).astimezone(_tz()).strftime("%Y-%m-%d")


def seconds_until_reset(now=None) -> int:
    local = (now or now_utc()).astimezone(_tz())
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - local).total_seconds()))


def fmt_dt(dt) -> str:
    if not dt:
        return "-"
    return dt.astimezone(_tz()).strftime("%d %b %Y, %I:%M %p %Z")


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"
    
