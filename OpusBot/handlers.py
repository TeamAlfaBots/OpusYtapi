"""Bot logic: user flow (/start, key card) + owner tools (/broadcast, /users, /revoke)."""
import asyncio
import csv
import io
import logging
import math
from datetime import timedelta

from pyrogram import errors, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import MessageNotModified, UserNotParticipant
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from OpusApi import config, security, utils
from OpusApi.database import mongo

from . import keyboards as K
from .strings import T, esc

log = logging.getLogger("opusbot")

PAGE_SIZE = 8
_tasks = set()
_broadcast_running = False

_BLOCK_ERRORS = tuple(
    getattr(errors, n)
    for n in ("UserIsBlocked", "InputUserDeactivated", "UserDeactivated", "UserDeactivatedBan", "PeerIdInvalid")
    if hasattr(errors, n)
)


# ── helpers ──────────────────────────────────────────────────
def full_name(user) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p) or str(user.id)


def key_state(doc) -> str:
    """none | active | expired | revoked"""
    if not doc:
        return "none"
    if doc.get("revoked"):
        return "revoked"
    if not doc.get("key_hash"):
        return "none"
    exp = doc.get("expires_at")
    return "expired" if (not exp or exp <= utils.now_utc()) else "active"


def render_card(doc, fresh: bool) -> str:
    key = security.decrypt_key(doc.get("key_enc"))
    return T(
        "key_generated" if fresh else "key_status",
        base_url=esc(config.BASE_URL or "-"),
        api_key=esc(key) if key else T("key_hidden"),
        valid_till=utils.fmt_dt(doc.get("expires_at")),
        used=mongo.view_used(doc),
        limit=int(doc.get("daily_limit", config.DAILY_LIMIT)),
        plan=esc(str(doc.get("plan", config.PLAN_NAME)).capitalize()),
        status=T("status_active" if key_state(doc) == "active" else "status_expired"),
    )


async def _show(cq, text, markup):
    """Edit the current message (caption if it carries media, text otherwise)."""
    msg = cq.message
    if msg.media:
        await msg.edit_caption(text, reply_markup=markup)
    else:
        await msg.edit_text(text, reply_markup=markup)


async def missing_channels(client, user_id: int):
    """-> (missing_usernames, verification_failed)"""
    missing = []
    for ch in config.FORCE_CHANNELS:
        try:
            member = await client.get_chat_member(f"@{ch}", user_id)
            ok = member.status in (
                ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
            ) or (member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", True))
            if not ok:
                missing.append(ch)
        except UserNotParticipant:
            missing.append(ch)
        except Exception as e:  # bot not admin in the channel, wrong username...
            log.error("join check failed for @%s: %s", ch, e)
            return [], True
    return missing, False


# ── start media (image / gif / video, any one) ──────────────
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
_VID_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
_media_kind_cache = {}


def _media_kind(media: str, forced: str):
    """-> list of kinds to try, in order: 'photo' | 'video' | 'gif'."""
    forced = (forced or "auto").lower()
    if forced in ("photo", "image", "img"):
        return ["photo"]
    if forced == "video":
        return ["video"]
    if forced in ("gif", "animation"):
        return ["gif"]
    path = media.split("?", 1)[0].lower()  # auto: detect from the URL
    if path.endswith(".gif"):
        return ["gif"]
    if path.endswith(_VID_EXT):
        return ["video"]
    if path.endswith(_IMG_EXT):
        return ["photo"]
    return ["photo", "gif", "video"]  # file_id / URL without extension: try each


async def _send_start_media(client, chat_id, text, markup) -> bool:
    """Send /start with the configured media. True if sent, False -> caller sends plain text."""
    media, mtype = config.START_MEDIA, config.START_MEDIA_TYPE
    if not media or mtype == "none":
        return False
    kinds = _media_kind(media, mtype)
    if media in _media_kind_cache:  # remember what worked last time
        kinds = [_media_kind_cache[media]]
    senders = {"photo": client.send_photo, "video": client.send_video, "gif": client.send_animation}
    for kind in kinds:
        try:
            await senders[kind](chat_id, media, caption=text, reply_markup=markup)
            _media_kind_cache[media] = kind
            return True
        except Exception as e:
            log.warning("start media as %s failed: %s", kind, e)
    _media_kind_cache.pop(media, None)
    return False


# ── user flow ────────────────────────────────────────────────
async def start_cmd(client, message):
    user = message.from_user
    if not user:
        return
    await mongo.touch_user(user.id, full_name(user), user.username)
    text, markup = T("start", name=esc(full_name(user))), K.start_menu()
    if await _send_start_media(client, message.chat.id, text, markup):
        return
    await client.send_message(message.chat.id, text, reply_markup=markup)


async def _join_gate(client, cq) -> bool:
    """True when the user may continue. Otherwise it already answered/edited."""
    if not config.FORCE_CHANNELS:
        return True
    missing, failed = await missing_channels(client, cq.from_user.id)
    if failed:
        await cq.answer(T("join_error"), show_alert=True)
        return False
    if missing:
        text = T("join_required", channels="\n".join(f"• @{c}" for c in missing))
        try:
            await _show(cq, text, K.join_menu(missing))
            await cq.answer()
        except MessageNotModified:
            await cq.answer(T("join_still"), show_alert=True)
        return False
    return True


async def _gen(client, cq, check_join: bool):
    user = cq.from_user
    if check_join and not await _join_gate(client, cq):
        return
    doc = await mongo.get_user(user.id)
    fresh = False
    if key_state(doc) in ("none", "revoked"):
        if not check_join:  # a stale button must never skip the channel check
            await cq.answer(T("renew_nokey"), show_alert=True)
            return
        doc = await mongo.issue_key(user.id, full_name(user), user.username)
        fresh = True
    await _show(cq, render_card(doc, fresh), K.key_menu())
    await cq.answer()


async def on_callback(client, cq):
    data = cq.data or ""
    user = cq.from_user
    try:
        if data.startswith("users:"):
            if user.id not in config.OWNER_IDS:
                await cq.answer(T("not_allowed"), show_alert=True)
                return
            text, markup = await _users_page(int(data.split(":", 1)[1]))
            await _show(cq, text, markup)
            await cq.answer()
        elif data == "gen":
            await _gen(client, cq, check_join=True)
        elif data == "card":
            await _gen(client, cq, check_join=False)
        elif data == "renew":
            if not await _join_gate(client, cq):
                return
            doc = await mongo.renew_key(user.id)
            if not doc:
                await cq.answer(T("renew_nokey"), show_alert=True)
                return
            await _show(cq, render_card(doc, fresh=False), K.key_menu())
            await cq.answer(T("renewed", days=config.KEY_VALID_DAYS))
        elif data == "revoke":
            await _show(cq, T("revoke_confirm"), K.revoke_confirm_menu())
            await cq.answer()
        elif data == "revoke_yes":
            if not await mongo.revoke_user(user.id):
                await cq.answer(T("revoke_none"), show_alert=True)
                return
            await _show(cq, T("revoked"), K.revoked_menu())
            await cq.answer()
        elif data == "back":
            await _show(cq, T("start", name=esc(full_name(user))), K.start_menu())
            await cq.answer()
        elif data == "close":
            await cq.message.delete()
        else:
            await cq.answer()
    except MessageNotModified:
        await cq.answer(T("not_modified"))
    except Exception as e:
        log.exception("callback %r failed: %s", data, e)
        try:
            await cq.answer(T("generic_error"), show_alert=True)
        except Exception:
            pass


# ── owner tools ──────────────────────────────────────────────
async def _users_page(page: int):
    counts = await mongo.user_counts()
    if not counts["total"]:
        return T("owner_users_empty"), None
    pages = max(1, math.ceil(counts["total"] / PAGE_SIZE))
    page = min(max(1, page), pages)
    docs = await mongo.list_users((page - 1) * PAGE_SIZE, PAGE_SIZE)
    text = T("owner_users_header", page=page, pages=pages, **counts)
    for d in docs:
        text += T(
            "owner_user_row",
            name=esc((d.get("name") or "?")[:40]),
            username=("@" + esc(d["username"])) if d.get("username") else "",
            uid=d["_id"],
            key_state=T("key_state_" + key_state(d)),
            generated=utils.fmt_dt(d.get("key_created_at")),
            expires=utils.fmt_dt(d.get("expires_at")),
            used=mongo.view_used(d),
            limit=int(d.get("daily_limit", config.DAILY_LIMIT)),
            total=int(d.get("total_requests", 0)),
        )
    return text, K.users_nav(page, pages)


async def users_cmd(client, message):
    if len(message.command) > 1 and message.command[1].lower() == "export":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "name", "username", "key_state", "key_created", "expires", "used_today",
                    "daily_limit", "total_requests", "blocked"])
        for d in await mongo.all_users():
            w.writerow([d["_id"], d.get("name", ""), d.get("username") or "", key_state(d),
                        utils.fmt_dt(d.get("key_created_at")), utils.fmt_dt(d.get("expires_at")),
                        mongo.view_used(d), d.get("daily_limit", config.DAILY_LIMIT),
                        d.get("total_requests", 0), bool(d.get("blocked"))])
        buf = io.BytesIO(out.getvalue().encode("utf-8-sig"))
        buf.name = "users.csv"
        await message.reply_document(buf)
        return
    text, markup = await _users_page(1)
    await message.reply_text(text, reply_markup=markup)


async def revoke_cmd(client, message):
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        await message.reply_text(T("owner_revoke_usage"), reply_markup=K.close_menu())
        return
    uid = int(message.command[1])
    if not await mongo.revoke_user(uid):
        await message.reply_text(T("owner_revoke_none", uid=uid), reply_markup=K.close_menu())
        return
    await message.reply_text(T("owner_revoke_ok", uid=uid), reply_markup=K.close_menu())
    try:
        await client.send_message(uid, T("user_revoked_by_owner"), reply_markup=K.notice_menu())
    except Exception:
        pass


async def setlimit_cmd(client, message):
    a = message.command
    if len(a) < 3 or not a[1].lstrip("-").isdigit() or not a[2].isdigit():
        await message.reply_text(T("owner_setlimit_usage"), reply_markup=K.close_menu())
        return
    uid, limit = int(a[1]), int(a[2])
    if await mongo.set_limit(uid, limit):
        await message.reply_text(T("owner_setlimit_ok", uid=uid, limit=limit), reply_markup=K.close_menu())
    else:
        await message.reply_text(T("owner_setlimit_none", uid=uid), reply_markup=K.close_menu())


async def _send_one(client, uid, from_chat, msg_id, text) -> str:
    for _ in range(2):
        try:
            if text is not None:
                await client.send_message(uid, text)
            else:
                await client.copy_message(uid, from_chat, msg_id)
            return "sent"
        except errors.FloodWait as e:
            await asyncio.sleep(getattr(e, "value", getattr(e, "x", 5)) + 1)
        except _BLOCK_ERRORS:
            await mongo.set_blocked(uid, True)
            return "blocked"
        except Exception as e:
            log.warning("broadcast to %s failed: %s", uid, e)
            return "failed"
    return "failed"


async def _run_broadcast(client, status, ids, from_chat, msg_id, text):
    global _broadcast_running
    stats = {"sent": 0, "blocked": 0, "failed": 0}
    try:
        for i, uid in enumerate(ids, 1):
            stats[await _send_one(client, uid, from_chat, msg_id, text)] += 1
            if i % 25 == 0:
                try:
                    await status.edit_text(T("owner_broadcast_progress", done=i, total=len(ids), **stats))
                except Exception:
                    pass
            await asyncio.sleep(config.BROADCAST_DELAY)
        await status.edit_text(T("owner_broadcast_done", total=len(ids), **stats))
    except Exception as e:
        log.exception("broadcast crashed: %s", e)
    finally:
        _broadcast_running = False


async def broadcast_cmd(client, message):
    global _broadcast_running
    reply = message.reply_to_message
    parts = (message.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else None
    if not reply and text is None:
        await message.reply_text(T("owner_broadcast_usage"), reply_markup=K.close_menu())
        return
    if _broadcast_running:
        await message.reply_text(T("owner_broadcast_busy"))
        return
    _broadcast_running = True
    try:
        ids = await mongo.broadcast_ids()
        status = await message.reply_text(T("owner_broadcast_start", total=len(ids)))
    except Exception:
        _broadcast_running = False
        raise
    task = asyncio.create_task(
        _run_broadcast(client, status, ids, message.chat.id, reply.id if reply else None,
                       None if reply else text)
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ── expiry reminders ─────────────────────────────────────────
async def reminder_loop(client):
    while True:
        try:
            await asyncio.sleep(120)
            now = utils.now_utc()
            for doc in await mongo.users_to_remind(now, timedelta(days=config.REMINDER_DAYS)):
                try:
                    await client.send_message(
                        doc["_id"],
                        T("reminder", valid_till=utils.fmt_dt(doc["expires_at"])),
                        reply_markup=K.reminder_menu(),
                    )
                except _BLOCK_ERRORS:
                    await mongo.set_blocked(doc["_id"], True)
                except Exception as e:
                    log.warning("reminder to %s failed: %s", doc["_id"], e)
                await mongo.mark_reminded(doc["_id"], doc["expires_at"])
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("reminder loop error: %s", e)
        await asyncio.sleep(3600)


# ── wiring ───────────────────────────────────────────────────
def register(app):
    private = filters.private
    app.add_handler(MessageHandler(start_cmd, filters.command("start") & private))
    app.add_handler(CallbackQueryHandler(on_callback))
    if config.OWNER_IDS:
        owner = private & filters.user(config.OWNER_IDS)
        app.add_handler(MessageHandler(broadcast_cmd, filters.command("broadcast") & owner))
        app.add_handler(MessageHandler(users_cmd, filters.command("users") & owner))
        app.add_handler(MessageHandler(revoke_cmd, filters.command("revoke") & owner))
        app.add_handler(MessageHandler(setlimit_cmd, filters.command("setlimit") & owner))
