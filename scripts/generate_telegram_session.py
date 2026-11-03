"""Interactively generate a Pyrogram user session string for the Telegram cache.

Set API_ID and API_HASH in the environment first. The printed session string is
an account credential: never share it or commit it to source control.
"""
import asyncio
import os

from pyrogram import Client


async def main():
    api_id = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id.isdigit() or not api_hash:
        raise SystemExit("Set API_ID and API_HASH environment variables first.")
    app = Client(
        "opus_cache_session_generator",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True,
    )
    await app.start()  # asks for phone number, login code and 2FA password if enabled
    try:
        session = await app.export_session_string()
        print("\nTELEGRAM_SESSION (keep secret; paste into your hosting env only):\n")
        print(session)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
