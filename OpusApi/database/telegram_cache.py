"""Telegram-backed persistent audio cache.

Audio files are posted to a private Telegram channel by a dedicated user session.
MongoDB stores only the lookup metadata (video ID -> channel message ID).
The channel is storage, not a public distribution endpoint.
"""
import logging
import os
import re
import time
from pathlib import Path

from .. import config
from . import mongo

log = logging.getLogger("opusapi.telegram_cache")
_client = None
_started = False


def enabled() -> bool:
    return bool(config.TELEGRAM_SESSION and config.TELEGRAM_CACHE_CHANNEL and config.API_ID and config.API_HASH)


async def start() -> bool:
    """Start the dedicated Pyrogram user session. Non-fatal if not configured."""
    global _client, _started
    if not enabled():
        log.info("Telegram song cache disabled (configure TELEGRAM_SESSION and TELEGRAM_CACHE_CHANNEL)")
        return False
    try:
        from pyrogram import Client
        _client = Client(
            "opus_telegram_cache",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=config.TELEGRAM_SESSION,
            in_memory=True,
            no_updates=True,
        )
        await _client.start()
        # Resolve channel at startup so bad IDs/permissions are noticed early.
        await _client.get_chat(_chat_id())
        await mongo.db()["song_cache_telegram"].create_index(
            [("video_id", 1), ("type", 1)], unique=True
        )
        await mongo.db()["song_cache_telegram"].create_index("updated_at")
        _started = True
        log.info("Telegram song cache connected to configured channel")
        return True
    except Exception as e:
        log.exception("Telegram cache could not start: %s", e)
        if _client is not None:
            try:
                await _client.stop()
            except Exception:
                pass
        _client = None
        _started = False
        return False


async def stop() -> None:
    global _client, _started
    if _client is not None:
        try:
            await _client.stop()
        except Exception as e:
            log.warning("Telegram cache shutdown issue: %s", e)
    _client = None
    _started = False


def _chat_id():
    value = config.TELEGRAM_CACHE_CHANNEL
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _collection():
    return mongo.db()["song_cache_telegram"]


async def get_cached_song(video_id: str, type_: str = "audio"):
    """Download a cached channel message to a local temp file; returns {path, ext} or None."""
    if not _started or type_ != "audio":
        return None
    doc = await _collection().find_one({"video_id": video_id, "type": type_})
    if not doc:
        return None
    try:
        message = await _client.get_messages(_chat_id(), int(doc["message_id"]))
        if not message or getattr(message, "empty", False):
            await _collection().delete_one({"_id": doc["_id"]})
            return None
        media = message.document or message.audio
        if not media:
            await _collection().delete_one({"_id": doc["_id"]})
            return None
        ext = re.sub(r"[^a-z0-9]", "", (doc.get("ext") or "mp3").lower())[:5] or "mp3"
        cache_dir = Path(config.CACHE_DIR)
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = str(cache_dir / f"{video_id}.tgcache.{ext}")
        result = await _client.download_media(message, file_name=target)
        if not result or not os.path.isfile(result) or os.path.getsize(result) <= 0:
            raise RuntimeError("Telegram returned no downloaded file")
        await _collection().update_one({"_id": doc["_id"]}, {"$set": {"last_accessed": int(time.time())}})
        return {"path": result, "ext": ext}
    except Exception as e:
        log.warning("Telegram cache read failed for %s: %s", video_id, e)
        # Keep the index entry for transient Telegram/network failures.
        return None


CHUNK = 1024 * 1024  # Telegram's download chunk size (stream_media works in chunks of 1 MiB)


async def open_stream(video_id: str):
    """Look up a cached song for direct streaming. Downloads NOTHING.

    -> {"message", "size", "ext"} or None (not cached / Telegram unavailable).
    """
    if not _started:
        return None
    try:
        doc = await _collection().find_one({"video_id": video_id, "type": "audio"})
        if not doc:
            return None
        message = await _client.get_messages(_chat_id(), int(doc["message_id"]))
        media = None if (not message or getattr(message, "empty", False)) else (message.audio or message.document)
        if media is None:
            await _collection().delete_one({"_id": doc["_id"]})  # message was deleted: forget it
            return None
        size = int(getattr(media, "file_size", 0) or doc.get("size_bytes") or 0)
        if size <= 0:
            return None
        ext = re.sub(r"[^a-z0-9]", "", (doc.get("ext") or "mp3").lower())[:5] or "mp3"
        await _collection().update_one({"_id": doc["_id"]}, {"$set": {"last_accessed": int(time.time())}})
        return {"message": message, "size": size, "ext": ext}
    except Exception as e:
        log.warning("Telegram stream lookup failed for %s: %s", video_id, e)
        return None


async def iter_range(message, start: int, end: int):
    """Yield the bytes start..end (inclusive) of a cached song, straight from Telegram."""
    first = start // CHUNK
    skip = start - first * CHUNK
    need = end - start + 1
    chunks = (skip + need + CHUNK - 1) // CHUNK
    async for chunk in _client.stream_media(message, limit=chunks, offset=first):
        if skip:
            chunk = chunk[skip:]
            skip = 0
        if len(chunk) > need:
            chunk = chunk[:need]
        if not chunk:
            continue
        need -= len(chunk)
        yield chunk
        if need <= 0:
            break


async def save_song(video_id: str, type_: str, ext: str, path: str) -> bool:
    """Upload a fresh audio file to the private cache channel and index its message."""
    if not _started or type_ != "audio" or not os.path.isfile(path):
        return False
    col = _collection()
    try:
        existing = await col.find_one({"video_id": video_id, "type": type_})
        if existing:
            # Avoid duplicate channel uploads when another request already stored it.
            return True
        size = os.path.getsize(path)
        caption = f"opus-cache:v1\nvideo_id={video_id}\ntype=audio\next={ext}\nsize={size}"
        msg = await _client.send_document(
            chat_id=_chat_id(),
            document=path,
            caption=caption,
            disable_notification=True,
        )
        doc = {
            "video_id": video_id,
            "type": type_,
            "ext": ext,
            "size_bytes": size,
            "message_id": int(msg.id),
            "chat_id": str(config.TELEGRAM_CACHE_CHANNEL),
            "created_at": int(time.time()),
            "last_accessed": int(time.time()),
        }
        await col.update_one(
            {"video_id": video_id, "type": type_},
            {"$setOnInsert": doc},
            upsert=True,
        )
        log.info("SAVED TO TELEGRAM CACHE: %s (%d bytes, message %s)", video_id, size, msg.id)
        return True
    except Exception as e:
        log.warning("Telegram cache save failed for %s: %s", video_id, e)
        return False


async def get_cache_stats() -> dict:
    if not _started:
        return {"enabled": enabled(), "status": "disabled" if not enabled() else "disconnected"}
    try:
        col = _collection()
        count = await col.count_documents({"type": "audio"})
        pipeline = [{"$match": {"type": "audio"}}, {"$group": {"_id": None, "bytes": {"$sum": "$size_bytes"}}}]
        rows = await col.aggregate(pipeline).to_list(length=1)
        size = int(rows[0]["bytes"]) if rows else 0
        return {"enabled": True, "status": "ok", "songs": count, "size_mb": round(size / 1048576, 2)}
    except Exception as e:
        return {"enabled": True, "status": "error", "error": str(e)[:120]}
        
