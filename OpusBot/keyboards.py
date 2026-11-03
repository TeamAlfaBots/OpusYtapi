from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

from OpusApi import config

from .strings import B


def _url(label_key: str, url: str):
    return Btn(B(label_key), url=url) if url else None  # no URL in env -> no button


def _rows(rows):
    rows = [[b for b in row if b] for row in rows]
    rows = [r for r in rows if r]
    return Markup(rows) if rows else None


def start_menu():
    return _rows([
        [Btn(B("generate"), callback_data="gen")],
        [_url("support", config.SUPPORT_URL), _url("updates", config.UPDATES_URL)],
        [_url("owner", config.OWNER_URL), _url("developer", config.DEVELOPER_URL)],
    ])


def join_menu(missing):
    rows = [[Btn(B("join", name=ch), url=f"https://t.me/{ch}")] for ch in missing]
    rows.append([Btn(B("joined"), callback_data="gen")])
    rows.append([Btn(B("back"), callback_data="back")])
    return _rows(rows)


def key_menu():
    return _rows([
        [Btn(B("revoke"), callback_data="revoke"), Btn(B("renew"), callback_data="renew")],
        [_url("report", config.REPORT_URL)],
        [Btn(B("back"), callback_data="back"), Btn(B("close"), callback_data="close")],
    ])


def revoke_confirm_menu():
    return _rows([[
        Btn(B("confirm_revoke"), callback_data="revoke_yes"),
        Btn(B("cancel"), callback_data="card"),
    ]])


def revoked_menu():
    return _rows([
        [Btn(B("generate_new"), callback_data="gen")],
        [Btn(B("back"), callback_data="back"), Btn(B("close"), callback_data="close")],
    ])


def users_nav(page: int, pages: int):
    row = []
    if page > 1:
        row.append(Btn(B("prev"), callback_data=f"users:{page - 1}"))
    if page < pages:
        row.append(Btn(B("next"), callback_data=f"users:{page + 1}"))
    return _rows([row])


def reminder_menu():
    return _rows([
        [Btn(B("renew_now"), callback_data="renew")],
        [Btn(B("open_key"), callback_data="card"), Btn(B("close"), callback_data="close")],
    ])


def notice_menu():
    return _rows([
        [Btn(B("generate_new"), callback_data="gen")],
        [_url("support", config.SUPPORT_URL)],
        [Btn(B("close"), callback_data="close")],
    ])


def close_menu():
    return _rows([[Btn(B("close"), callback_data="close")]])
