"""MongoDB layer: users, API keys, daily quota.

One document per Telegram user (_id = telegram id). All state lives here, so a
server restart loses nothing. Quota is enforced with ONE atomic update.
"""
import logging
import time
from datetime import timedelta

from .. import config, security, utils

log = logging.getLogger("opusapi.mongo")

_client = None
_db = None


def db():
    global _client, _db
    if _db is None:
        if not config.MONGO_URI:
            raise RuntimeError("MONGO_URI is not set")
        from motor.motor_asyncio import AsyncIOMotorClient

        # ONE shared client (Atlas free allows only 500 connections).
        _client = AsyncIOMotorClient(
            config.MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=8000, maxPoolSize=20
        )
        _db = _client[config.MONGO_DB]
    return _db


def users():
    return db()["users"]


async def init_mongo() -> bool:
    try:
        await users().create_index(
            "key_hash", unique=True, partialFilterExpression={"key_hash": {"$type": "string"}}
        )
        await users().create_index("expires_at")
        log.info("MongoDB ready")
        return True
    except Exception as e:  # never block startup
        log.error("MongoDB init failed: %s", e)
        return False


def _defaults(now) -> dict:
    return {
        "created_at": now,
        "plan": config.PLAN_NAME.lower(),
        "daily_limit": config.DAILY_LIMIT,
        "used_today": 0,
        "day": utils.today_str(now),
        "total_requests": 0,
    }


# ── key lookup (short in-memory cache, protects Mongo from hammering) ─────────
_KEY_CACHE = {}
_KEY_TTL = 30


def clear_key_cache():
    _KEY_CACHE.clear()


async def lookup_key(key: str):
    """-> (state, tg_id). state: ok | invalid | revoked | expired."""
    h = security.hash_key(key)
    now = time.monotonic()
    hit = _KEY_CACHE.get(h)
    if hit and hit[0] > now:
        doc = hit[1]
    else:
        doc = await users().find_one({"key_hash": h}, {"revoked": 1, "expires_at": 1})
        if len(_KEY_CACHE) > 20000:
            _KEY_CACHE.clear()
        _KEY_CACHE[h] = (now + _KEY_TTL, doc)
    if not doc:
        return "invalid", None
    if doc.get("revoked"):
        return "revoked", None
    exp = doc.get("expires_at")
    if not exp or exp <= utils.now_utc():
        return "expired", doc["_id"]
    return "ok", doc["_id"]


# ── user / key management (used by the bot) ──────────────────
async def get_user(tg_id: int):
    return await users().find_one({"_id": tg_id})


async def touch_user(tg_id: int, name: str, username):
    now = utils.now_utc()
    await users().update_one(
        {"_id": tg_id},
        {
            "$set": {"name": name, "username": username, "last_seen": now, "blocked": False},
            "$setOnInsert": _defaults(now),
        },
        upsert=True,
    )


async def issue_key(tg_id: int, name: str, username):
    """Create a brand-new key (first time, or after a revoke). Quota state is kept."""
    key = security.generate_api_key()
    now = utils.now_utc()
    await users().update_one(
        {"_id": tg_id},
        {
            "$set": {
                "name": name,
                "username": username,
                "key_hash": security.hash_key(key),
                "key_enc": security.encrypt_key(key),
                "key_created_at": now,
                "expires_at": now + timedelta(days=config.KEY_VALID_DAYS),
                "revoked": False,
                "last_seen": now,
                "blocked": False,
            },
            "$unset": {"revoked_at": "", "reminded_for": ""},
            "$setOnInsert": _defaults(now),
        },
        upsert=True,
    )
    clear_key_cache()
    return await get_user(tg_id)


async def renew_key(tg_id: int):
    """Same key, validity restarts from now. Returns the new doc or None."""
    from pymongo import ReturnDocument

    doc = await users().find_one_and_update(
        {"_id": tg_id, "revoked": {"$ne": True}, "key_hash": {"$type": "string"}},
        {
            "$set": {"expires_at": utils.now_utc() + timedelta(days=config.KEY_VALID_DAYS)},
            "$unset": {"reminded_for": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    clear_key_cache()
    return doc


async def revoke_user(tg_id: int) -> bool:
    res = await users().update_one(
        {"_id": tg_id, "key_hash": {"$type": "string"}},
        {
            "$set": {"revoked": True, "revoked_at": utils.now_utc()},
            "$unset": {"key_hash": "", "key_enc": ""},
        },
    )
    clear_key_cache()
    return res.modified_count > 0


async def set_limit(tg_id: int, limit: int) -> bool:
    res = await users().update_one({"_id": tg_id}, {"$set": {"daily_limit": int(limit)}})
    return res.matched_count > 0


async def set_blocked(tg_id: int, flag: bool):
    try:
        await users().update_one({"_id": tg_id}, {"$set": {"blocked": bool(flag)}})
    except Exception as e:
        log.warning("set_blocked failed: %s", e)


# ── quota (atomic) ───────────────────────────────────────────
async def consume(tg_id: int) -> dict:
    """Charge 1 request. One atomic update: validates the key, resets the
    counter when the day changed, and refuses when the limit is reached.

    -> {"state": "ok"|"invalid"|"expired"|"quota", "used": int, "limit": int}
    """
    from pymongo import ReturnDocument

    now = utils.now_utc()
    today = utils.today_str(now)
    used = {"$ifNull": ["$used_today", 0]}
    limit = {"$ifNull": ["$daily_limit", config.DAILY_LIMIT]}

    flt = {
        "_id": tg_id,
        "revoked": {"$ne": True},
        "key_hash": {"$type": "string"},
        "expires_at": {"$gt": now},
        "$expr": {"$or": [{"$ne": ["$day", today]}, {"$lt": [used, limit]}]},
    }
    update = [
        {
            "$set": {
                "used_today": {
                    "$cond": [{"$ne": ["$day", today]}, 1, {"$add": [used, 1]}]
                },
                "day": today,
                "total_requests": {"$add": [{"$ifNull": ["$total_requests", 0]}, 1]},
                "last_used": now,
            }
        }
    ]
    doc = await users().find_one_and_update(
        flt, update, return_document=ReturnDocument.AFTER,
        projection={"used_today": 1, "daily_limit": 1},
    )
    if doc:
        return {
            "state": "ok",
            "used": int(doc.get("used_today", 1)),
            "limit": int(doc.get("daily_limit", config.DAILY_LIMIT)),
        }

    cur = await users().find_one({"_id": tg_id}, {"revoked": 1, "key_hash": 1, "expires_at": 1, "daily_limit": 1})
    if not cur or cur.get("revoked") or not cur.get("key_hash"):
        return {"state": "invalid", "used": 0, "limit": 0}
    exp = cur.get("expires_at")
    if not exp or exp <= now:
        return {"state": "expired", "used": 0, "limit": 0}
    lim = int(cur.get("daily_limit", config.DAILY_LIMIT))
    return {"state": "quota", "used": lim, "limit": lim}


async def refund(tg_id: int):
    """Give back 1 request (download failed / server busy)."""
    try:
        dec = lambda f: {"$max": [0, {"$subtract": [{"$ifNull": [f, 0]}, 1]}]}  # noqa: E731
        await users().update_one(
            {"_id": tg_id, "day": utils.today_str()},
            [{"$set": {"used_today": dec("$used_today"), "total_requests": dec("$total_requests")}}],
        )
    except Exception as e:
        log.warning("refund failed for %s: %s", tg_id, e)


# ── owner / bot queries ──────────────────────────────────────
def view_used(doc) -> int:
    """used_today as the user should see it (0 if the day already rolled over)."""
    return int(doc.get("used_today", 0)) if doc.get("day") == utils.today_str() else 0


async def user_counts() -> dict:
    now = utils.now_utc()
    u = users()
    has_key = {"key_hash": {"$type": "string"}}
    out = {
        "total": await u.count_documents({}),
        "with_key": await u.count_documents(has_key),
        "active": await u.count_documents({**has_key, "revoked": {"$ne": True}, "expires_at": {"$gt": now}}),
        "revoked": await u.count_documents({"revoked": True}),
        "blocked": await u.count_documents({"blocked": True}),
    }
    agg = await u.aggregate(
        [{"$match": {"day": utils.today_str(now)}}, {"$group": {"_id": None, "n": {"$sum": "$used_today"}}}]
    ).to_list(1)
    out["today_requests"] = int(agg[0]["n"]) if agg else 0
    return out


async def list_users(skip: int, limit: int) -> list:
    return await users().find({}).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)


async def all_users() -> list:
    return await users().find({}).sort("created_at", 1).to_list(length=None)


async def broadcast_ids() -> list:
    docs = await users().find({"blocked": {"$ne": True}}, {"_id": 1}).to_list(length=None)
    return [d["_id"] for d in docs]


async def users_to_remind(now, within) -> list:
    return await users().find(
        {
            "revoked": {"$ne": True},
            "blocked": {"$ne": True},
            "key_hash": {"$type": "string"},
            "expires_at": {"$gt": now, "$lte": now + within},
            "$expr": {"$ne": [{"$ifNull": ["$reminded_for", None]}, "$expires_at"]},
        },
        {"_id": 1, "expires_at": 1},
    ).to_list(length=500)


async def mark_reminded(tg_id: int, expires_at):
    await users().update_one({"_id": tg_id}, {"$set": {"reminded_for": expires_at}})


_TOTALS = {"at": 0.0, "value": {"users": 0, "total_requests": 0}}


async def get_totals() -> dict:
    """Lifetime totals for /stats (cached 60s)."""
    if time.monotonic() - _TOTALS["at"] > 60:
        agg = await users().aggregate(
            [{"$group": {"_id": None, "n": {"$sum": "$total_requests"}, "u": {"$sum": 1}}}]
        ).to_list(1)
        _TOTALS["value"] = {
            "users": int(agg[0]["u"]) if agg else 0,
            "total_requests": int(agg[0]["n"]) if agg else 0,
        }
        _TOTALS["at"] = time.monotonic()
    return _TOTALS["value"]
