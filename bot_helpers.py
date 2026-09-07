import asyncio
import os
import random
import re
import string
from datetime import datetime, timezone

import asyncpg
import discord

CURRENCY_NAME = "Dollars"
CURRENCY_SYMBOL = "$"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return int(str(raw).strip())


def _env_int_set(name: str, default: set[int]) -> set[int]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return set(default)
    return {int(part.strip()) for part in str(raw).split(",") if part.strip()}


ALLOWED_CHANNEL_ID = _env_int("ALLOWED_CHANNEL_ID", 1494473065281617971)
DAILY_AMOUNT = 50
STARTING_BALANCE = 250
MIN_BATTLE_WAGER = 100
MIN_TEAM_BET = 1
CHALLENGE_TIMEOUT_SECONDS = 300
MODERATOR_ROLE_ID = _env_int("MODERATOR_ROLE_ID", 1494455406691483658)
LOG_CHANNEL_ID = _env_int("LOG_CHANNEL_ID", 1494449437240463451)
QUEUE_CHANNEL_IDS = _env_int_set(
    "QUEUE_CHANNEL_IDS",
    {1478102174541025451, 989621653703098398},
)
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


class InsufficientFundsError(Exception):
    """Raised when a balance/escrow mutation cannot complete safely."""


class PayoutReverseError(Exception):
    """Raised when a completed match payout cannot be safely reversed."""


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
    row = await conn.fetchrow(
        """
        UPDATE users
        SET escrow = escrow + $1
        WHERE user_id = $2 AND balance - escrow >= $1
        RETURNING balance, escrow
        """,
        amount,
        str(user_id),
    )
    if not row:
        raise InsufficientFundsError(
            f"Insufficient available funds to escrow {fmt(amount)} for user {user_tag or user_id}."
        )
    await log(
        f"🔒 ESCROW HELD — {user_tag or user_id} escrowed {fmt(amount)} ({reason}) | Available: {fmt(row['balance'] - row['escrow'])}"
    )


async def release_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    row = await conn.fetchrow(
        """
        UPDATE users
        SET escrow = escrow - $1
        WHERE user_id = $2 AND escrow >= $1
        RETURNING balance, escrow
        """,
        amount,
        str(user_id),
    )
    if not row:
        raise InsufficientFundsError(
            f"Cannot release {fmt(amount)} escrow for user {user_tag or user_id}; escrow too low."
        )
    await log(f"🔓 ESCROW RELEASED — {user_tag or user_id} refunded {fmt(amount)} ({reason})")


async def release_escrow_up_to(conn, user_id: int, amount: int, reason: str, user_tag: str = "") -> int:
    """
    Release up to `amount` from escrow without failing if escrow is short.
    Used by admin clear/cancel flows so inconsistent escrow can't block cleanup.
    Returns the amount actually released.
    """
    if amount <= 0:
        return 0

    row = await lock_user(conn, user_id)
    if not row:
        await log(
            f"⚠️ ESCROW RELEASE SKIPPED — {user_tag or user_id} not found ({reason}; wanted {fmt(amount)})"
        )
        return 0

    available = max(0, row["escrow"])
    to_release = min(amount, available)
    if to_release <= 0:
        await log(
            f"⚠️ ESCROW RELEASE SKIPPED — {user_tag or user_id} had {fmt(0)} escrow "
            f"({reason}; wanted {fmt(amount)})"
        )
        return 0

    updated = await conn.fetchrow(
        """
        UPDATE users
        SET escrow = escrow - $1
        WHERE user_id = $2 AND escrow >= $1
        RETURNING balance, escrow
        """,
        to_release,
        str(user_id),
    )
    if not updated:
        await log(
            f"⚠️ ESCROW RELEASE SKIPPED — {user_tag or user_id} escrow changed concurrently "
            f"({reason}; wanted {fmt(amount)})"
        )
        return 0

    note = ""
    if to_release < amount:
        note = f" (only {fmt(to_release)} available; wanted {fmt(amount)})"
    await log(
        f"🔓 ESCROW RELEASED — {user_tag or user_id} refunded {fmt(to_release)} ({reason}){note}"
    )
    return to_release


async def burn_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    row = await conn.fetchrow(
        """
        UPDATE users
        SET balance = balance - $1, escrow = escrow - $1
        WHERE user_id = $2 AND balance >= $1 AND escrow >= $1
        RETURNING balance, escrow
        """,
        amount,
        str(user_id),
    )
    if not row:
        raise InsufficientFundsError(
            f"Cannot burn {fmt(amount)} escrow for user {user_tag or user_id}; funds too low."
        )
    await log(f"🔥 ESCROW BURNED — {user_tag or user_id} lost {fmt(amount)} ({reason})")


async def lock_match(conn, match_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM matches WHERE match_id = $1 FOR UPDATE",
        match_id,
    )


async def lock_user(conn, user_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM users WHERE user_id = $1 FOR UPDATE",
        str(user_id),
    )


async def lock_users_ordered(conn, *user_ids: int) -> dict[int, asyncpg.Record | None]:
    """Lock users in ascending ID order to avoid deadlocks."""
    ordered = sorted({int(uid) for uid in user_ids})
    locked: dict[int, asyncpg.Record | None] = {}
    for uid in ordered:
        locked[uid] = await lock_user(conn, uid)
    return locked


async def complete_match_if_active(
    conn,
    match_id: str,
    winner_id: str,
    reported_by_id: str,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        UPDATE matches
        SET status = 'COMPLETED', winner_id = $1, reported_by_id = $2
        WHERE match_id = $3 AND status = 'ACTIVE'
        RETURNING *
        """,
        winner_id,
        reported_by_id,
        match_id,
    )


async def cancel_match_if_pending(conn, match_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        UPDATE matches
        SET status = 'CANCELLED'
        WHERE match_id = $1 AND status = 'PENDING'
        RETURNING *
        """,
        match_id,
    )

def has_mod_role(ctx: discord.ApplicationContext) -> bool:
    if isinstance(ctx.author, discord.Member):
        return any(r.id == MODERATOR_ROLE_ID for r in ctx.author.roles)
    return False


def member_has_mod_role(user: discord.Member | discord.User | None) -> bool:
    if isinstance(user, discord.Member):
        return any(r.id == MODERATOR_ROLE_ID for r in user.roles)
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

    last_error = None
    for attempt in range(3):
        try:
            msg = await channel.fetch_message(int(match["message_id"]))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.35 * (attempt + 1))
    await log(
        f"⚠️ MATCH MESSAGE UPDATE FAILED — Match ID: {match_id} | Error: {last_error}"
    )


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


async def build_open_matches_embed(page: int = 1) -> tuple[discord.Embed, int]:
    """Build a paginated embed of PENDING / ACCEPTED / ACTIVE matches."""
    page_size = 10
    async with get_db_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT match_id, challenger_id, opponent_id, wager_amount, status, created_at
            FROM matches
            WHERE status IN ('PENDING', 'ACCEPTED', 'ACTIVE')
            ORDER BY
                CASE status
                    WHEN 'ACTIVE' THEN 1
                    WHEN 'ACCEPTED' THEN 2
                    WHEN 'PENDING' THEN 3
                    ELSE 4
                END,
                created_at ASC
            """
        )

    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]

    status_counts = {"ACTIVE": 0, "ACCEPTED": 0, "PENDING": 0}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    embed = discord.Embed(
        title="📋 Open Matches",
        color=discord.Color.teal(),
    )
    embed.set_footer(
        text=(
            f"Page {page}/{total_pages} • {total} open • "
            f"Active {status_counts['ACTIVE']} • Accepted {status_counts['ACCEPTED']} • Pending {status_counts['PENDING']}"
        )
    )

    if not page_rows:
        embed.description = "There are no open player battles right now."
    else:
        lines = []
        for row in page_rows:
            challenger = await get_display_name(row["challenger_id"])
            opponent = await get_display_name(row["opponent_id"])
            status = row["status"]
            if status == "ACTIVE":
                status_label = "🟢 ACTIVE"
            elif status == "ACCEPTED":
                status_label = "🟡 ACCEPTED"
            else:
                status_label = "🟠 PENDING"

            lines.append(
                f"**`{row['match_id']}`** · {status_label}\n"
                f"{challenger} vs {opponent} · Wager {fmt(row['wager_amount'])}"
            )
        embed.description = "\n\n".join(lines)

    async with get_db_pool().acquire() as conn:
        team_rows = await conn.fetch(
            """
            SELECT match_id, role_one_id, role_two_id, status
            FROM team_matches
            WHERE status IN ('OPEN', 'ACTIVE')
            ORDER BY
                CASE status WHEN 'ACTIVE' THEN 1 WHEN 'OPEN' THEN 2 ELSE 3 END,
                created_at ASC
            LIMIT 10
            """
        )

    if team_rows:
        team_lines = []
        for row in team_rows:
            status_label = "🟢 ACTIVE" if row["status"] == "ACTIVE" else "🟠 OPEN"
            team_lines.append(
                f"**`{row['match_id']}`** · {status_label}\n"
                f"{await get_role_mention(row['role_one_id'])} vs {await get_role_mention(row['role_two_id'])}"
            )
        embed.add_field(
            name="Team Matches",
            value="\n\n".join(team_lines),
            inline=False,
        )

    return embed, total_pages


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
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_matches (
            match_id TEXT PRIMARY KEY,
            role_one_id TEXT NOT NULL,
            role_two_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            winner_role_id TEXT,
            created_by_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_bets (
            bet_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL REFERENCES team_matches(match_id),
            bettor_id TEXT NOT NULL,
            predicted_role_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS bets_one_pending_per_user
        ON bets (match_id, bettor_id)
        WHERE status = 'PENDING'
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS team_bets_one_pending_per_user
        ON team_bets (match_id, bettor_id)
        WHERE status = 'PENDING'
        """
    )


async def restore_match_to_pre_payout(conn, match_id: str, previous_winner_id: str):
    match = await lock_match(conn, match_id)
    if not match:
        raise PayoutReverseError(f"Match `{match_id}` not found.")
    if match["status"] != "COMPLETED":
        raise PayoutReverseError(
            f"Match `{match_id}` must be COMPLETED to reverse a payout (current: {match['status']})."
        )

    wager = match["wager_amount"]
    challenger_id = match["challenger_id"]
    opponent_id = match["opponent_id"]
    previous_loser_id = challenger_id if previous_winner_id == opponent_id else opponent_id

    previous_winner = await get_bot().fetch_user(int(previous_winner_id))
    previous_loser = await get_bot().fetch_user(int(previous_loser_id))

    winner_row = await lock_user(conn, int(previous_winner_id))
    if not winner_row or winner_row["balance"] < wager:
        have = winner_row["balance"] if winner_row else 0
        raise PayoutReverseError(
            f"Cannot reverse match `{match_id}`: {fmt_user(previous_winner)} only has {fmt(have)} "
            f"but needs {fmt(wager)} still available to return winnings."
        )

    bets = await conn.fetch("SELECT * FROM bets WHERE match_id = $1", match_id)
    for bet in bets:
        if bet["status"] != "WON":
            continue
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        bettor_row = await lock_user(conn, int(bet["bettor_id"]))
        if not bettor_row or bettor_row["balance"] < bet["amount"]:
            have = bettor_row["balance"] if bettor_row else 0
            raise PayoutReverseError(
                f"Cannot reverse match `{match_id}`: {fmt_user(bettor)} only has {fmt(have)} "
                f"but needs {fmt(bet['amount'])} still available to return bet winnings."
            )

    winner_updated = await conn.fetchrow(
        """
        UPDATE users
        SET balance = balance - $1, escrow = escrow + $1
        WHERE user_id = $2 AND balance >= $1
        RETURNING user_id
        """,
        wager,
        str(previous_winner_id),
    )
    if not winner_updated:
        raise PayoutReverseError(
            f"Cannot reverse match `{match_id}`: failed to reclaim winnings from {fmt_user(previous_winner)}."
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

    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        if bet["status"] == "WON":
            updated = await conn.fetchrow(
                """
                UPDATE users
                SET balance = balance - $1, escrow = escrow + $1
                WHERE user_id = $2 AND balance >= $1
                RETURNING user_id
                """,
                bet["amount"],
                bet["bettor_id"],
            )
            if not updated:
                raise PayoutReverseError(
                    f"Cannot reverse match `{match_id}`: failed to reclaim bet winnings from {fmt_user(bettor)}."
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


async def run_payout(conn, match_id: str, winner_id: str, mod_tag: str | None = None) -> discord.Embed:
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

    await log(
        f"🏆 MATCH COMPLETED — Match ID: {match_id} | Winner: {fmt_user(winner_user)} | Loser: {fmt_user(loser_user)} | Wager: {fmt(wager)}"
        + (f" | Force-resolved by: {mod_tag}" if mod_tag else "")
    )
    return embed


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


async def cancel_open_match_with_refunds(conn, match) -> tuple[int, int]:
    """
    Cancel one PENDING/ACCEPTED/ACTIVE match and refund escrows + pending bets.
    Returns (player_refunds, bet_refunds).
    Uses release_escrow_up_to so short/inconsistent escrow cannot abort the clear.
    """
    match_id = match["match_id"]
    status = match["status"]
    wager = match["wager_amount"]

    cancelled = await conn.fetchrow(
        """
        UPDATE matches
        SET status = 'CANCELLED'
        WHERE match_id = $1 AND status IN ('PENDING', 'ACCEPTED', 'ACTIVE')
        RETURNING *
        """,
        match_id,
    )
    if not cancelled:
        return 0, 0

    player_refunds = 0
    challenger_id = int(match["challenger_id"])
    opponent_id = int(match["opponent_id"])
    challenger = await get_bot().fetch_user(challenger_id)

    if status == "PENDING":
        released = await release_escrow_up_to(
            conn,
            challenger_id,
            wager,
            f"open matches cleared (match {match_id})",
            fmt_user(challenger),
        )
        if released > 0:
            player_refunds = 1
    else:
        opponent = await get_bot().fetch_user(opponent_id)
        released_challenger = await release_escrow_up_to(
            conn,
            challenger_id,
            wager,
            f"open matches cleared (match {match_id})",
            fmt_user(challenger),
        )
        released_opponent = await release_escrow_up_to(
            conn,
            opponent_id,
            wager,
            f"open matches cleared (match {match_id})",
            fmt_user(opponent),
        )
        player_refunds = int(released_challenger > 0) + int(released_opponent > 0)

    bets = await conn.fetch(
        """
        SELECT * FROM bets
        WHERE match_id = $1 AND status = 'PENDING'
        FOR UPDATE
        """,
        match_id,
    )
    bet_refunds = 0
    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        released = await release_escrow_up_to(
            conn,
            int(bet["bettor_id"]),
            bet["amount"],
            f"bet refunded — open matches cleared (match {match_id})",
            fmt_user(bettor),
        )
        await conn.execute("DELETE FROM bets WHERE bet_id = $1", bet["bet_id"])
        if released > 0:
            bet_refunds += 1

    return player_refunds, bet_refunds


async def lock_team_match(conn, match_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM team_matches WHERE match_id = $1 FOR UPDATE",
        match_id,
    )


async def get_team_bet_totals(conn, match_id: str) -> dict[str, int]:
    rows = await conn.fetch(
        """
        SELECT predicted_role_id, COALESCE(SUM(amount), 0) AS total, COUNT(*)::int AS bet_count
        FROM team_bets
        WHERE match_id = $1 AND status = 'PENDING'
        GROUP BY predicted_role_id
        """,
        match_id,
    )
    return {row["predicted_role_id"]: int(row["total"]) for row in rows}


async def get_role_mention(role_id: str | int) -> str:
    return f"<@&{role_id}>"


async def build_team_match_embed(
    match_id: str,
    role_one_id: int,
    role_two_id: int,
    status: str,
    *,
    created_by_text: str | None = None,
    winner_role_id: int | None = None,
) -> discord.Embed:
    async with get_db_pool().acquire() as conn:
        totals = await get_team_bet_totals(conn, match_id)
        pending_bets = await conn.fetchval(
            "SELECT COUNT(*) FROM team_bets WHERE match_id = $1 AND status = 'PENDING'",
            match_id,
        ) or 0

    role_one_total = totals.get(str(role_one_id), 0)
    role_two_total = totals.get(str(role_two_id), 0)

    if status == "OPEN":
        title = "🏟️ Team Match — Bets Open"
        color = discord.Color.orange()
        status_text = "Place a bet with the buttons below. A moderator will start the match to lock bets."
    elif status == "ACTIVE":
        title = "🏟️ Team Match — In Progress"
        color = discord.Color.blue()
        status_text = "Bets are locked. A moderator will declare the winner when the match ends."
    elif status == "COMPLETED":
        title = "🏟️ Team Match — Complete"
        color = discord.Color.gold()
        winner = await get_role_mention(winner_role_id) if winner_role_id else "Unknown"
        status_text = f"Winner: {winner}"
    else:
        title = "🏟️ Team Match — Cancelled"
        color = discord.Color.dark_gray()
        status_text = "This team match was cancelled. Pending bets were refunded."

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Team 1", value=await get_role_mention(role_one_id), inline=True)
    embed.add_field(name="Team 2", value=await get_role_mention(role_two_id), inline=True)
    embed.add_field(name="Match ID", value=f"`{match_id}`", inline=True)
    embed.add_field(name="Bets on Team 1", value=fmt(role_one_total), inline=True)
    embed.add_field(name="Bets on Team 2", value=fmt(role_two_total), inline=True)
    embed.add_field(name="Open Bets", value=str(pending_bets), inline=True)
    embed.add_field(name="Status", value=status_text, inline=False)
    if created_by_text:
        embed.set_footer(text=created_by_text)
    return embed


async def update_team_match_message(match_id: str, embed: discord.Embed, view: discord.ui.View | None = None):
    async with get_db_pool().acquire() as conn:
        match = await conn.fetchrow(
            "SELECT channel_id, message_id FROM team_matches WHERE match_id = $1",
            match_id,
        )
    if not match or not match["message_id"]:
        return

    channel = get_bot().get_channel(int(match["channel_id"]))
    if channel is None:
        try:
            channel = await get_bot().fetch_channel(int(match["channel_id"]))
        except Exception:
            return

    last_error = None
    for attempt in range(3):
        try:
            msg = await channel.fetch_message(int(match["message_id"]))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.35 * (attempt + 1))
    await log(
        f"⚠️ TEAM MATCH MESSAGE UPDATE FAILED — Match ID: {match_id} | Error: {last_error}"
    )


async def run_team_match_payout(conn, match_id: str, winner_role_id: str, mod_tag: str | None = None) -> discord.Embed:
    match = await conn.fetchrow("SELECT * FROM team_matches WHERE match_id = $1", match_id)
    role_one_id = match["role_one_id"]
    role_two_id = match["role_two_id"]
    loser_role_id = role_two_id if winner_role_id == role_one_id else role_one_id

    bets = await conn.fetch(
        "SELECT * FROM team_bets WHERE match_id = $1 AND status = 'PENDING'",
        match_id,
    )
    bet_lines = []
    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        if bet["predicted_role_id"] == winner_role_id:
            await release_escrow(
                conn,
                int(bet["bettor_id"]),
                bet["amount"],
                "team bet won (escrow released)",
                fmt_user(bettor),
            )
            await credit(
                conn,
                int(bet["bettor_id"]),
                bet["amount"],
                "team bet won (winnings)",
                fmt_user(bettor),
            )
            await conn.execute("UPDATE team_bets SET status = 'WON' WHERE bet_id = $1", bet["bet_id"])
            bet_lines.append(
                f"  ✅ {fmt_user(bettor)} bet {fmt(bet['amount'])} on {await get_role_mention(winner_role_id)} → Won {fmt(bet['amount'])}"
            )
        else:
            await burn_escrow(
                conn,
                int(bet["bettor_id"]),
                bet["amount"],
                "team bet lost",
                fmt_user(bettor),
            )
            await conn.execute("UPDATE team_bets SET status = 'LOST' WHERE bet_id = $1", bet["bet_id"])
            bet_lines.append(
                f"  ❌ {fmt_user(bettor)} bet {fmt(bet['amount'])} on {await get_role_mention(loser_role_id)} → Lost"
            )

    embed = discord.Embed(title="🏟️ Team Match Complete!", color=discord.Color.gold())
    embed.add_field(name="Match ID", value=match_id, inline=True)
    embed.add_field(name="Winner", value=await get_role_mention(winner_role_id), inline=True)
    if bet_lines:
        # Discord field value max 1024 chars
        text = "\n".join(bet_lines)
        if len(text) > 1000:
            text = text[:1000] + "\n…"
        embed.add_field(name="Bet Outcomes", value=text, inline=False)
    else:
        embed.add_field(name="Bet Outcomes", value="No bets were placed.", inline=False)
    if mod_tag:
        embed.set_footer(text=f"Resolved by mod: {mod_tag}")

    await log(
        f"🏆 TEAM MATCH COMPLETED — Match ID: {match_id} | Winner role: {winner_role_id} | Bets: {len(bets)}"
        + (f" | By: {mod_tag}" if mod_tag else "")
    )
    return embed


async def cancel_team_match_with_refunds(conn, match_id: str) -> int:
    """Cancel an OPEN/ACTIVE team match and refund pending bets. Returns bet refund count."""
    cancelled = await conn.fetchrow(
        """
        UPDATE team_matches
        SET status = 'CANCELLED'
        WHERE match_id = $1 AND status IN ('OPEN', 'ACTIVE')
        RETURNING *
        """,
        match_id,
    )
    if not cancelled:
        return 0

    bets = await conn.fetch(
        """
        SELECT * FROM team_bets
        WHERE match_id = $1 AND status = 'PENDING'
        FOR UPDATE
        """,
        match_id,
    )
    refunded = 0
    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        released = await release_escrow_up_to(
            conn,
            int(bet["bettor_id"]),
            bet["amount"],
            f"team bet refunded (match {match_id})",
            fmt_user(bettor),
        )
        await conn.execute("DELETE FROM team_bets WHERE bet_id = $1", bet["bet_id"])
        if released > 0:
            refunded += 1
    return refunded
