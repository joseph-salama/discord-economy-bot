import os

import asyncpg
import discord

from bot_commands import register_commands
from bot_helpers import init_database, log, reward_queue_match, set_bot, set_db_pool

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = discord.Bot(intents=intents)

set_bot(bot)
register_commands(bot)


@bot.event
async def on_ready():
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    set_db_pool(db_pool)
    async with db_pool.acquire() as conn:
        await init_database(conn)
    print(f"✅ Logged in as {bot.user} | DB connected")
    await log(f"🤖 Bot started and ready — {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    try:
        reward_queue_match
    except Exception:
        return
    from bot_helpers import get_db_pool
    try:
        get_db_pool()
    except RuntimeError:
        return
    await reward_queue_match(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    from bot_helpers import get_db_pool
    try:
        get_db_pool()
    except RuntimeError:
        return
    await reward_queue_match(after)


bot.run(DISCORD_TOKEN)
