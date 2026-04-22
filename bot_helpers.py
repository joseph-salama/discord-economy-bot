import random
import re
import string
from datetime import datetime, timezone

import asyncpg
import discord

CURRENCY_NAME = "Dollars"
CURRENCY_SYMBOL = "$"
ALLOWED_CHANNEL_ID = 1494473065281617971
DAILY_AMOUNT = 50
STARTING_BALANCE = 250
MIN_BATTLE_WAGER = 100
CHALLENGE_TIMEOUT_SECONDS = 300
MODERATOR_ROLE_ID = 1494455406691483658
LOG_CHANNEL_ID = 1494449437240463451
QUEUE_CHANNEL_IDS = {1478102174541025451, 989621653703098398}
MATCH_REWARD = 100
TOP_PAGE_SIZE = 8

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


async def credit(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
        amount,
        str(user_id),
    )
    row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", str(user_id))
    await log(
        f"💰 BALANCE CREDITED — {user_tag or user_id} received {fmt(amount)} ({reason}) | New balance: {fmt(row['balance'])}"
    )


async def debit_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET escrow = escrow + $1 WHERE user_id = $2",
        amount,
        str(user_id),
    )
    row = await conn.fetchrow("SELECT balance, escrow FROM users WHERE user_id = $1", str(user_id))
    await log(
        f"🔒 ESCROW HELD — {user_tag or user_id} escrowed {fmt(amount)} ({reason}) | Available: {fmt(row['balance'] - row['escrow'])}"
    )


async def release_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET escrow = escrow - $1 WHERE user_id = $2",
        amount,
        str(user_id),
    )
    await log(f"🔓 ESCROW RELEASED — {user_tag or user_id} refunded {fmt(amount)} ({reason})")


async def burn_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET balance = balance - $1, escrow = escrow - $1 WHERE user_id = $2",
        amount,
        str(user_id),
    )
    await log(f"🔥 ESCROW BURNED — {user_tag or user_id} lost {fmt(amount)} ({reason})")


def has_mod_role(ctx: discord.ApplicationContext) -> bool:
    if isinstance(ctx.author, discord.Member):
        return any(r.id == MODERATOR_ROLE_ID for r in ctx.author.roles)
    return False


async def enforce_channel(ctx: discord.ApplicationContext) -> bool:
    if ALLOWED_CHANNEL_ID and ctx.channel_id != ALLOWED_CHANNEL_ID:
        allowed_mention = f"<#{ALLOWED_CHANNEL_ID}>"
        await ctx.respond(f"This bot only works in {allowed_mention}.", ephemeral=True)
        return False
    return True


async def get_display_name(user_id: str) -> str:
    try:
        user = get_bot().get_user(int(user_id)) or await get_bot().fetch_user(int(user_id))
        return user.display_name if hasattr(user, "display_name") else user.name
    except Exception:
        return f"User {user_id}"


async def build_top_embed(page: int) -> tuple[discord.Embed, int]:
    async with get_db_pool().acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        total_pages = max(1, (total_users + TOP_PAGE_SIZE - 1) // TOP_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * TOP_PAGE_SIZE

        rows = await conn.fetch(
            """
            SELECT user_id, balance, escrow
            FROM users
            ORDER BY balance DESC, user_id ASC
            LIMIT $1 OFFSET $2
            """,
            TOP_PAGE_SIZE,
            offset,
        )

    embed = discord.Embed(
        title=f"🏆 {CURRENCY_NAME} Leaderboard",
        color=discord.Color.gold(),
    )

    if not rows:
        embed.description = f"No one has any {CURRENCY_NAME} yet."
    else:
        lines = []
        for index, row in enumerate(rows, start=offset + 1):
            name = await get_display_name(row["user_id"])
            available = row["balance"] - row["escrow"]
            lines.append(
                f"**#{index}** {name} — Total: {fmt(row['balance'])} | Available: {fmt(available)}"
            )
        embed.description = "\n".join(lines)

    embed.set_footer(text=f"Page {page}/{total_pages} • {TOP_PAGE_SIZE} per page")
    return embed, total_pages


async def update_match_message(match_id: str, embed: discord.Embed, view: discord.ui.View | None = None):
    async with get_db_pool().acquire() as conn:
        match = await conn.fetchrow("SELECT channel_id, message_id FROM matches WHERE match_id = $1", match_id)
    if not match or not match["message_id"]:
        return

    channel = get_bot().get_channel(int(match["channel_id"]))
    if channel is None:
        try:
            channel = await get_bot().fetch_channel(int(match["channel_id"]))
        except Exception:
            return

    try:
        msg = await channel.fetch_message(int(match["message_id"]))
        await msg.edit(embed=embed, view=view)
    except Exception:
        pass


async def build_accepted_match_embed(
    match_id: str,
    challenger_id: int,
    opponent_id: int,
    wager_amount: int,
    accepted_by_text: str | None = None,
) -> discord.Embed:
    challenger = await get_bot().fetch_user(challenger_id)
    opponent = await get_bot().fetch_user(opponent_id)
    embed = discord.Embed(
        title="⚔️ Challenge Accepted!",
        description=f"{challenger.mention} vs {opponent.mention}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Wager", value=fmt(wager_amount), inline=True)
    embed.add_field(name="Match ID", value=match_id, inline=True)
    embed.add_field(
        name="Status",
        value="Bets are now open! Use `/bet` to wager on a player.\nWhen ready, either player or a moderator can press **Start Match** below or use `/start`.",
        inline=False,
    )
    if accepted_by_text:
        embed.set_footer(text=accepted_by_text)
    return embed


async def build_cancelled_match_embed(description: str) -> discord.Embed:
    return discord.Embed(
        title="❌ Battle Cancelled",
        description=description,
        color=discord.Color.dark_gray(),
    )


async def find_open_match_between(conn, user_one_id: int, user_two_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT *
        FROM matches
        WHERE (
            (challenger_id = $1 AND opponent_id = $2)
            OR
            (challenger_id = $2 AND opponent_id = $1)
        )
        AND status IN ('PENDING', 'ACCEPTED', 'ACTIVE')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        str(user_one_id),
        str(user_two_id),
    )


async def init_database(conn):
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            escrow INTEGER NOT NULL DEFAULT 0,
            last_daily TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY,
            challenger_id TEXT NOT NULL,
            opponent_id TEXT NOT NULL,
            wager_amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            winner_id TEXT,
            reported_by_id TEXT,
            channel_id TEXT NOT NULL,
            message_id TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            accepted_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            bet_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL REFERENCES matches(match_id),
            bettor_id TEXT NOT NULL,
            predicted_winner_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rewarded_queue_messages (
            message_id TEXT PRIMARY KEY,
            rewarded_at TIMESTAMPTZ NOT NULL
        )
        """
    )


async def restore_match_to_pre_payout(conn, match_id: str, previous_winner_id: str):
    match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    wager = match["wager_amount"]
    challenger_id = match["challenger_id"]
    opponent_id = match["opponent_id"]
    previous_loser_id = challenger_id if previous_winner_id == opponent_id else opponent_id

    previous_winner = await get_bot().fetch_user(int(previous_winner_id))
    previous_loser = await get_bot().fetch_user(int(previous_loser_id))

    await conn.execute(
        "UPDATE users SET balance = balance - $1, escrow = escrow + $1 WHERE user_id = $2",
        wager,
        str(previous_winner_id),
    )
    await log(
        f"↩️ MATCH PAYOUT REVERSED — {fmt_user(previous_winner)} returned {fmt(wager)} winnings and had {fmt(wager)} re-escrowed (match {match_id})"
    )

    await conn.execute(
        "UPDATE users SET balance = balance + $1, escrow = escrow + $1 WHERE user_id = $2",
        wager,
        str(previous_loser_id),
    )
    await log(
        f"↩️ MATCH PAYOUT REVERSED — {fmt_user(previous_loser)} had {fmt(wager)} restored and re-escrowed (match {match_id})"
    )

    bets = await conn.fetch("SELECT * FROM bets WHERE match_id = $1", match_id)
    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        if bet["status"] == "WON":
            await conn.execute(
                "UPDATE users SET balance = balance - $1, escrow = escrow + $1 WHERE user_id = $2",
                bet["amount"],
                bet["bettor_id"],
            )
            await log(
                f"↩️ BET PAYOUT REVERSED — {fmt_user(bettor)} returned {fmt(bet['amount'])} winnings and had {fmt(bet['amount'])} re-escrowed (match {match_id})"
            )
        elif bet["status"] == "LOST":
            await conn.execute(
                "UPDATE users SET balance = balance + $1, escrow = escrow + $1 WHERE user_id = $2",
                bet["amount"],
                bet["bettor_id"],
            )
            await log(
                f"↩️ BET PAYOUT REVERSED — {fmt_user(bettor)} had {fmt(bet['amount'])} restored and re-escrowed (match {match_id})"
            )

    await conn.execute(
        "UPDATE bets SET status = 'PENDING' WHERE match_id = $1 AND status IN ('WON', 'LOST')",
        match_id,
    )


async def run_payout(conn, match_id: str, winner_id: str, channel: discord.abc.Messageable, mod_tag: str | None = None):
    match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    wager = match["wager_amount"]
    challenger_id = match["challenger_id"]
    opponent_id = match["opponent_id"]
    loser_id = challenger_id if winner_id == opponent_id else opponent_id

    winner_user = await get_bot().fetch_user(int(winner_id))
    loser_user = await get_bot().fetch_user(int(loser_id))

    await release_escrow(conn, int(winner_id), wager, "battle win escrow released", fmt_user(winner_user))
    await credit(conn, int(winner_id), wager, "battle win (opponent wager)", fmt_user(winner_user))
    await burn_escrow(conn, int(loser_id), wager, "battle loss", fmt_user(loser_user))

    bets = await conn.fetch("SELECT * FROM bets WHERE match_id = $1 AND status = 'PENDING'", match_id)
    bet_lines = []
    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        if bet["predicted_winner_id"] == winner_id:
            await release_escrow(conn, int(bet["bettor_id"]), bet["amount"], "bet won (escrow released)", fmt_user(bettor))
            await credit(conn, int(bet["bettor_id"]), bet["amount"], "bet won (winnings)", fmt_user(bettor))
            await conn.execute("UPDATE bets SET status = 'WON' WHERE bet_id = $1", bet["bet_id"])
            bet_lines.append(
                f"  ✅ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{fmt_user(winner_user)}** → Won {fmt(bet['amount'])}"
            )
        else:
            await burn_escrow(conn, int(bet["bettor_id"]), bet["amount"], "bet lost", fmt_user(bettor))
            await conn.execute("UPDATE bets SET status = 'LOST' WHERE bet_id = $1", bet["bet_id"])
            bet_lines.append(
                f"  ❌ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{fmt_user(loser_user)}** → Lost"
            )

    embed = discord.Embed(title="⚔️ Match Complete!", color=discord.Color.gold())
    embed.add_field(name="Match ID", value=match_id, inline=True)
    embed.add_field(name="Winner", value=winner_user.mention, inline=True)
    embed.add_field(name="Payout", value=fmt(wager * 2), inline=True)
    if bet_lines:
        embed.add_field(name="Bet Outcomes", value="\n".join(bet_lines), inline=False)
    if mod_tag:
        embed.set_footer(text=f"Force-resolved by mod: {mod_tag}")
    await channel.send(embed=embed)

    await log(
        f"🏆 MATCH COMPLETED — Match ID: {match_id} | Winner: {fmt_user(winner_user)} | Loser: {fmt_user(loser_user)} | Wager: {fmt(wager)}"
        + (f" | Force-resolved by: {mod_tag}" if mod_tag else "")
    )


def parse_team_mentions(content: str) -> list[int]:
    team_section = re.search(
        r"Team 1\s*\n([\s\S]*?)Team 2\s*\n([\s\S]*?)(?:Match ID|$)",
        content,
        re.IGNORECASE,
    )
    if not team_section:
        return []

    team1_block = team_section.group(1)
    team2_block = team_section.group(2)

    ids: list[int] = []
    for block in (team1_block, team2_block):
        ids.extend(int(m) for m in re.findall(r"<@!?(\d+)>", block))

    return list(dict.fromkeys(ids))


async def reward_queue_match(message: discord.Message):
    if message.channel.id not in QUEUE_CHANNEL_IDS:
        return
    if not message.author.bot:
        return

    content = message.content or ""
    if not content:
        for embed in message.embeds:
            content += f"\n{embed.description or ''}"
            for field in embed.fields:
                content += f"\n{field.name}\n{field.value}"

    player_ids = parse_team_mentions(content)
    if not player_ids:
        return

    async with get_db_pool().acquire() as conn:
        already = await conn.fetchrow(
            "SELECT 1 FROM rewarded_queue_messages WHERE message_id = $1",
            str(message.id),
        )
        if already:
            return

        await conn.execute(
            "INSERT INTO rewarded_queue_messages (message_id, rewarded_at) VALUES ($1, $2)",
            str(message.id),
            now_utc(),
        )

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
