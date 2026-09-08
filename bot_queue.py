"""Queue-channel match reward parsing."""
import re

import discord

from bot_config import MATCH_REWARD, QUEUE_CHANNEL_IDS
from bot_ledger import credit
from bot_runtime import ensure_user, fmt, fmt_user, get_bot, get_db_pool, log, now_utc


def parse_team_mentions(content: str) -> list[int]:
    """Extract unique player IDs from Team 1 / Team 2 queue-bot text."""
    if not content:
        return []

    patterns = [
        # Classic multiline blocks ending at Match ID
        r"Team\s*1\s*:?\s*\n([\s\S]*?)Team\s*2\s*:?\s*\n([\s\S]*?)(?:Match\s*ID|$)",
        # Same-line or loosely separated team sections
        r"Team\s*1\s*:?\s*([\s\S]*?)Team\s*2\s*:?\s*([\s\S]*?)(?:Match\s*ID|$)",
        # Team headers without requiring Match ID terminator
        r"Team\s*1\s*:?\s*([\s\S]*?)Team\s*2\s*:?\s*([\s\S]+)",
    ]

    for pattern in patterns:
        team_section = re.search(pattern, content, re.IGNORECASE)
        if not team_section:
            continue
        ids: list[int] = []
        for block in team_section.groups():
            ids.extend(int(m) for m in re.findall(r"<@!?(\d+)>", block or ""))
        unique = list(dict.fromkeys(ids))
        if unique:
            return unique

    return []


async def reward_queue_match(message: discord.Message):
    if message.channel.id not in QUEUE_CHANNEL_IDS:
        return
    if not message.author.bot:
        return

    content = message.content or ""
    if not content:
        for embed in message.embeds:
            content += f"\n{embed.title or ''}"
            content += f"\n{embed.description or ''}"
            for field in embed.fields:
                content += f"\n{field.name}\n{field.value}"

    player_ids = parse_team_mentions(content)
    if not player_ids:
        looks_like_queue = bool(re.search(r"team\s*1", content, re.IGNORECASE)) and bool(
            re.search(r"team\s*2", content, re.IGNORECASE)
        )
        if looks_like_queue:
            await log(
                f"⚠️ QUEUE PARSE MISS — message {message.id} in #{getattr(message.channel, 'name', message.channel.id)} "
                "looked like a queue match but no player mentions were parsed."
            )
        return

    async with get_db_pool().acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """
                INSERT INTO rewarded_queue_messages (message_id, rewarded_at)
                VALUES ($1, $2)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
                """,
                str(message.id),
                now_utc(),
            )
            if not inserted:
                return

            rewarded_tags = []
            for uid in player_ids:
                await ensure_user(conn, uid)
                await credit(conn, uid, MATCH_REWARD, f"queue match reward (msg {message.id})")
                try:
                    user = await get_bot().fetch_user(uid)
                    rewarded_tags.append(fmt_user(user))
                except Exception:
                    rewarded_tags.append(str(uid))

    await log(
        f"🎮 QUEUE MATCH REWARD — {fmt(MATCH_REWARD)} granted to {len(player_ids)} players in #{message.channel.name}: {', '.join(rewarded_tags)}"
    )
