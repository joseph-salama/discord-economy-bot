"""Discord embeds, permissions helpers, and match lookups."""
import asyncio

import asyncpg
import discord

from bot_config import ALLOWED_CHANNEL_ID, CURRENCY_NAME, MODERATOR_ROLE_ID, TOP_PAGE_SIZE
from bot_runtime import fmt, get_bot, get_db_pool, log


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


async def find_open_team_match_between(conn, role_one_id: int, role_two_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT *
        FROM team_matches
        WHERE (
            (role_one_id = $1 AND role_two_id = $2)
            OR
            (role_one_id = $2 AND role_two_id = $1)
        )
        AND status IN ('OPEN', 'ACTIVE')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        str(role_one_id),
        str(role_two_id),
    )


def resolve_role_label(role_id: int, fallback: str = "Team") -> str:
    try:
        for guild in get_bot().guilds:
            role = guild.get_role(int(role_id))
            if role:
                return role.name
    except Exception:
        pass
    return fallback


async def resolve_user_label(user_id: int, fallback: str = "Player") -> str:
    try:
        user = get_bot().get_user(int(user_id)) or await get_bot().fetch_user(int(user_id))
        return user.name
    except Exception:
        return fallback

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

