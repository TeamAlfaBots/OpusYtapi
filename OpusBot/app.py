import asyncio
import logging

from OpusApi import config

from . import handlers

log = logging.getLogger("opusbot")


def build_bot():
    from pyrogram import Client, enums

    missing = [n for n, v in (("BOT_TOKEN", config.BOT_TOKEN), ("API_ID", config.API_ID),
                              ("API_HASH", config.API_HASH)) if not v]
    if missing:
        raise RuntimeError("Missing env: " + ", ".join(missing))
    bot = Client(
        "opus_manager",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        in_memory=True,   # no session file: nothing to lose on ephemeral disks
        parse_mode=enums.ParseMode.HTML,
    )
    handlers.register(bot)
    return bot


async def start_bot(bot):
    await bot.start()
    if config.REMINDER_DAYS > 0:
        return asyncio.create_task(handlers.reminder_loop(bot))
    return None


async def stop_bot(bot, task):
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await bot.stop()


async def amain():
    from pyrogram import idle

    from OpusApi.database import mongo

    await mongo.init_mongo()
    bot = build_bot()
    task = await start_bot(bot)
    log.info("OpusMusicAPI Manager bot is running")
    await idle()
    await stop_bot(bot, task)


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(amain())
