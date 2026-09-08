import random
import string
from datetime import datetime, timezone

import asyncpg
import discord

from bot_config import LOG_CHANNEL_ID, STARTING_BALANCE

_bot: discord.Bot | None = None
_db_pool: asyncpg.Pool | None = None


def set_bot(bot: discord.Bot):
    global _bot
    _bot = bot


def get_bot() -> discord.Bot:
    if _bot is None:
        raise RuntimeError("Bot has not been initialized yet.")
    return _bot


def set_db_pool(pool: asyncpg.Pool):
    global _db_pool
    _db_pool = pool


def get_db_pool() -> asyncpg.Pool:
    if _db_pool is None:
        raise RuntimeError("Database pool has not been initialized yet.")
    return _db_pool


def fmt(amount: int) -> str:
    from bot_config import CURRENCY_SYMBOL
    return f"{CURRENCY_SYMBOL}{amount}"


def gen_id(length: int = 5) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_user(user: discord.User | discord.Member) -> str:
    return f"{user.name}#{user.discriminator}" if user.discriminator != "0" else user.name


def ts() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")


async def log(message: str):
    try:
        channel = get_bot().get_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(f"`[{ts()}]` {message}")
    except Exception as e:
        print(f"Failed to log message: {e}\nMessage was: {message}")


async def ensure_user(conn, user_id: int):
    await conn.execute(
        """
        INSERT INTO users (user_id, balance, escrow, last_daily)
        VALUES ($1, $2, 0, NULL)
        ON CONFLICT (user_id) DO NOTHING
        """,
        str(user_id), STARTING_BALANCE,
    )


async def get_user(conn, user_id: int) -> asyncpg.Record:
    return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", str(user_id))


def spendable(record: asyncpg.Record) -> int:
    return record["balance"] - record["escrow"]
