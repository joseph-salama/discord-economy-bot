import os
import asyncpg
import discord
from discord.ext import tasks
from bot_commands import register_commands
from bot_helpers import init_database, log, reward_queue_match, set_bot, set_db_pool, get_db_pool
from bot_views import expire_stale_challenges, restore_persistent_views
from bot_team_matches import expire_stale_team_matches, restore_team_match_views

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN environment variable is required.")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL environment variable is required.")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = discord.Bot(intents=intents)

set_bot(bot)
register_commands(bot)

_db_ready = False

@tasks.loop(minutes=4)
async def keepalive():
    try:
        async with get_db_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:
        print(f"⚠️ Keepalive ping failed: {e}")

@tasks.loop(minutes=1)
async def challenge_expiry_sweeper():
    try:
        await expire_stale_challenges()
    except Exception as e:
        print(f"⚠️ Challenge expiry sweeper failed: {e}")
    try:
        await expire_stale_team_matches()
    except Exception as e:
        print(f"⚠️ Team match expiry sweeper failed: {e}")

@bot.event
async def on_ready():
    global _db_ready
    try:
        if not _db_ready:
            db_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                max_inactive_connection_lifetime=300
            )
            set_db_pool(db_pool)
            async with db_pool.acquire() as conn:
                await init_database(conn)
            _db_ready = True
            print(f"✅ Logged in as {bot.user} | DB connected")
            await log(f"🤖 Bot started and ready — {bot.user}")
        else:
            # Reconnect: reuse existing pool, verify it, refresh views
            async with get_db_pool().acquire() as conn:
                await conn.fetchval("SELECT 1")
            print(f"✅ Reconnected as {bot.user} | DB pool reused")

        await restore_persistent_views()
        await restore_team_match_views()
        if not keepalive.is_running():
            keepalive.start()
        if not challenge_expiry_sweeper.is_running():
            challenge_expiry_sweeper.start()
    except Exception as e:
        import traceback
        print(f"❌ FATAL on_ready error: {traceback.format_exc()}")
        print("❌ Shutting down because the database failed to initialize.")
        await bot.close()

@bot.event
async def on_message(message: discord.Message):
    try:
        get_db_pool()
    except RuntimeError:
        return
    await reward_queue_match(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    try:
        get_db_pool()
    except RuntimeError:
        return
    await reward_queue_match(after)

bot.run(DISCORD_TOKEN)
