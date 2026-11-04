import logging
import re

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

from OpusApi import config

from .strings import B

log = logging.getLogger("opusbot")


def _norm_url(raw: str):
    """Make an env value a valid Telegram button URL, or None (button is skipped).

    '@name' / 'name' -> https://t.me/name ; 't.me/x' -> 'https://t.me/x'.
    """
    v = (raw or "").strip().strip("\"'").strip()
    if not v or any(c in v for c in " <>\t\n"):
        return None
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", v):
        return f"https://t.me/{v.lstrip('@')}"
    if re.match(r"(?i)^(t\.me|telegram\.me|www\.)", v):
        v = "https://" + v
    if not re.match(r"(?i)^(https?://|tg://)", v):
        return None
    if re.fullmatch(r"(?i)https?://(www\.)?(t|telegram)\.me/?", v):  # t.me with no name
        return None
    if "." not in v and not v.lower().startswith("tg://"):
        return None
    return v



try:  # Bot API 9.4+ coloured buttons (PyroTGFork)
    from pyrogram.enums import ButtonStyle
except Exception:  # older library: buttons simply stay uncoloured
    ButtonStyle = None

_STYLE_OK = ButtonStyle is not None
# label key -> colour: success = green, danger = red, primary = blue
STYLES = {
    # /start menu, same look as the reference screenshot
    "generate": "success",            # green
    "support": "success",             # green
    "updates": "danger",              # red
    "owner": "primary",               # blue
    "developer": "primary",           # blue
    # other screens
    "generate_new": "success", "renew_now": "success", "joined": "success",
    "renew": "primary", "join": "primary", "open_key": "primary",
    "prev": "primary", "next": "primary",
    "revoke": "danger", "confirm_revoke": "danger",
}


def _btn(label_key: str, fmt=None, **kw):
    """Button with its label from en.json and, when supported, a Bot API colour."""
    global _STYLE_OK
    text = B(label_key, **(fmt or {}))
    style = STYLES.get(label_key)
    if style and _STYLE_OK and getattr(config, "BUTTON_STYLES", True):
        try:
            return Btn(text, style=getattr(ButtonStyle, style.upper()), **kw)
        except Exception as e:  # library without `style` support
            _STYLE_OK = False
            log.warning("Coloured buttons not supported by this library (%s); using plain buttons", e)
    return Btn(text, **kw)

def _norm_url(raw: str):
    """Make an env value a valid Telegram button URL, or None (button is skipped).

    '@name' / 'name' -> https://t.me/name ; 't.me/x' -> 'https://t.me/x'.
    """
    v = (raw or "").strip().strip("\"'").strip()
    if not v or any(c in v for c in " <>\t\n"):
        return None
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", v):
        return f"https://t.me/{v.lstrip('@')}"
    if re.match(r"(?i)^(t\.me|telegram\.me|www\.)", v):
        v = "https://" + v
    if not re.match(r"(?i)^(https?://|tg://)", v):
        return None
    if re.fullmatch(r"(?i)https?://(www\.)?(t|telegram)\.me/?", v):  # t.me with no name
        return None
    if "." not in v and not v.lower().startswith("tg://"):
        return None
    return v


def _url(label_key: str, url: str):
    fixed = _norm_url(url)
    if url and not fixed:
        log.warning("Ignoring invalid URL for %s button: %r", label_key, url)
    return _btn(label_key, url=fixed) if fixed else None  # no/invalid URL -> no button


def _rows(rows):
    rows = [[b for b in row if b] for row in rows]
    rows = [r for r in rows if r]
    return Markup(rows) if rows else None


def start_menu():
    return _rows([
        [_btn("generate", callback_data="gen")],
        [_url("support", config.SUPPORT_URL), _url("updates", config.UPDATES_URL)],
        [_url("owner", config.OWNER_URL), _url("developer", config.DEVELOPER_URL)],
    ])


def join_menu(missing):
    rows = [[_btn("join", {"name": ch}, url=f"https://t.me/{ch}")] for ch in missing]
    rows.append([_btn("joined", callback_data="gen")])
    rows.append([_btn("back", callback_data="back")])
    return _rows(rows)


def key_menu():
    return _rows([
        [_btn("revoke", callback_data="revoke"), _btn("renew", callback_data="renew")],
        [_url("report", config.REPORT_URL)],
        [_btn("back", callback_data="back"), _btn("close", callback_data="close")],
    ])


def revoke_confirm_menu():
    return _rows([[
        _btn("confirm_revoke", callback_data="revoke_yes"),
        _btn("cancel", callback_data="card"),
    ]])


def revoked_menu():
    return _rows([
        [_btn("generate_new", callback_data="gen")],
        [_btn("back", callback_data="back"), _btn("close", callback_data="close")],
    ])


def users_nav(page: int, pages: int):
    row = []
    if page > 1:
        row.append(_btn("prev", callback_data=f"users:{page - 1}"))
    if page < pages:
        row.append(_btn("next", callback_data=f"users:{page + 1}"))
    return _rows([row])


def reminder_menu():
    return _rows([
        [_btn("renew_now", callback_data="renew")],
        [_btn("open_key", callback_data="card"), _btn("close", callback_data="close")],
    ])


def notice_menu():
    return _rows([
        [_btn("generate_new", callback_data="gen")],
        [_url("support", config.SUPPORT_URL)],
        [_btn("close", callback_data="close")],
    ])


def close_menu():
    return _rows([[_btn("close", callback_data="close")]])
