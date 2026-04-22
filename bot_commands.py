from datetime import timedelta
import traceback

import discord
from discord import option

from bot_helpers import (
    CURRENCY_NAME,
    DAILY_AMOUNT,
    MIN_BATTLE_WAGER,
    build_accepted_match_embed,
    build_cancelled_match_embed,
    build_top_embed,
    credit,
    debit_escrow,
    enforce_channel,
    ensure_user,
    find_open_match_between,
    fmt,
    fmt_user,
    gen_id,
    get_bot,
    get_db_pool,
    get_user,
    has_mod_role,
    log,
    now_utc,
    release_escrow,
    restore_match_to_pre_payout,
    run_payout,
    spendable,
    update_match_message,
)
from bot_views import ChallengeView, MatchReportView, MatchStartView, TopLeaderboardView


def register_commands(bot: discord.Bot):
    @bot.slash_command(description="Show all commands available to you")
    async def help(ctx: discord.ApplicationContext):
        try:
            if not await enforce_channel(ctx):
                return

            commands_list = [
                "**/battle** — Challenge another player to a battle.",
                "**/start** — Start an accepted match.",
                "**/report** — Report the winner of your active match.",
                "**/bet** — Bet on a player in an accepted match.",
                "**/balance** — Check your balance or another user's balance.",
                f"**/give** — Give some of your {CURRENCY_NAME} to another user.",
                f"**/daily** — Claim your daily {fmt(DAILY_AMOUNT)} {CURRENCY_NAME}.",
                "**/top** — View the money leaderboard.",
                "**/cancelbattle** — Cancel a pending battle you created.",
                "**/cancelbet** — Cancel your pending bet before the match starts.",
            ]

            if has_mod_role(ctx):
                commands_list.extend([
                    "**/reset** — [MOD] Reset a user's balance and escrow to 0.",
                    "**/adjustbalance** — [MOD] Add or remove from a user's balance.",
                    "**/resolve** — [MOD] Force-resolve a match.",
                    "**/forcebattle** — [MOD] Create a battle for two users without needing acceptance.",
                    "**/forceaccept** — [MOD] Accept a pending battle for the users.",
                    "**/forcecancel** — [MOD] Cancel a pending battle for the users.",
                ])

            embed = discord.Embed(
                title="📖 Help",
                description="Here are the commands available to you:",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Commands", value="\n".join(commands_list), inline=False)
            embed.set_footer(text="This message is only visible to you.")

            await ctx.respond(embed=embed, ephemeral=True)

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /help | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description=f"Challenge another player to a {CURRENCY_NAME} battle")
    @option("opponent", discord.Member, description="The player you want to challenge")
    @option("amount", int, description=f"Wager amount in {CURRENCY_NAME}")
    async def battle(ctx: discord.ApplicationContext, opponent: discord.Member, amount: int):
        try:
            if not await enforce_channel(ctx):
                return
            if amount < MIN_BATTLE_WAGER:
                return await ctx.respond(f"The minimum battle wager is {fmt(MIN_BATTLE_WAGER)}.", ephemeral=True)
            if opponent.id == ctx.author.id:
                return await ctx.respond("You cannot battle yourself.", ephemeral=True)
            if opponent.bot:
                return await ctx.respond("You cannot battle a bot.", ephemeral=True)

            await ctx.defer()

            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                await ensure_user(conn, opponent.id)
                challenger_row = await get_user(conn, ctx.author.id)
                opponent_row = await get_user(conn, opponent.id)

                existing_match = await find_open_match_between(conn, ctx.author.id, opponent.id)
                if existing_match:
                    return await ctx.followup.send(
                        f"You already have an open battle with {opponent.mention} (match `{existing_match['match_id']}`, status: {existing_match['status']}). "
                        "You can start battles with other people, but only one unresolved battle is allowed per pair until it is completed, declined, cancelled, or times out.",
                        ephemeral=True,
                    )

                challenger_available = spendable(challenger_row)
                if challenger_available < amount:
                    return await ctx.followup.send(
                        f"You have insufficient funds. You need {fmt(amount)} but only have {fmt(challenger_available)} available.",
                        ephemeral=True,
                    )
                if spendable(opponent_row) < amount:
                    return await ctx.followup.send(
                        f"{opponent.display_name} doesn't have enough {CURRENCY_NAME} to match that wager.",
                        ephemeral=True,
                    )

                match_id = gen_id()
                while await conn.fetchrow("SELECT 1 FROM matches WHERE match_id = $1", match_id):
                    match_id = gen_id()

                await debit_escrow(conn, ctx.author.id, amount, f"battle wager escrowed (match {match_id})", fmt_user(ctx.author))
                await conn.execute(
                    """
                    INSERT INTO matches (match_id, challenger_id, opponent_id, wager_amount, status, channel_id, created_at)
                    VALUES ($1, $2, $3, $4, 'PENDING', $5, $6)
                    """,
                    match_id,
                    str(ctx.author.id),
                    str(opponent.id),
                    amount,
                    str(ctx.channel_id),
                    now_utc(),
                )

            embed = discord.Embed(title="⚔️ Battle Challenge!", color=discord.Color.orange())
            embed.add_field(name="Challenger", value=ctx.author.mention, inline=True)
            embed.add_field(name="Opponent", value=opponent.mention, inline=True)
            embed.add_field(name="Wager", value=fmt(amount), inline=True)
            embed.add_field(name="Match ID", value=match_id, inline=True)
            from bot_helpers import CHALLENGE_TIMEOUT_SECONDS
            embed.set_footer(text=f"Challenge expires in {CHALLENGE_TIMEOUT_SECONDS // 60} minutes")

            view = ChallengeView(match_id, ctx.author.id, opponent.id)
            msg = await ctx.followup.send(embed=embed, view=view, wait=True)
            if msg:
                async with get_db_pool().acquire() as conn:
                    await conn.execute("UPDATE matches SET message_id = $1 WHERE match_id = $2", str(msg.id), match_id)

            await log(
                f"⚔️ BATTLE CREATED — Challenger: {fmt_user(ctx.author)} vs Opponent: {fmt_user(opponent)} | Wager: {fmt(amount)} | Match ID: {match_id}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong. Please try again.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong. Please try again.", ephemeral=True)
            await log(f"❌ ERROR — Command: /battle | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Force-create a battle between two users")
    @option("player_one", discord.Member, description="The first player")
    @option("player_two", discord.Member, description="The second player")
    @option("amount", int, description=f"Wager amount in {CURRENCY_NAME}")
    async def forcebattle(ctx: discord.ApplicationContext, player_one: discord.Member, player_two: discord.Member, amount: int):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)
            if amount < MIN_BATTLE_WAGER:
                return await ctx.respond(f"The minimum battle wager is {fmt(MIN_BATTLE_WAGER)}.", ephemeral=True)
            if player_one.id == player_two.id:
                return await ctx.respond("You must choose two different users.", ephemeral=True)
            if player_one.bot or player_two.bot:
                return await ctx.respond("You cannot force a battle involving a bot.", ephemeral=True)

            await ctx.defer(ephemeral=True)

            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, player_one.id)
                await ensure_user(conn, player_two.id)
                player_one_row = await get_user(conn, player_one.id)
                player_two_row = await get_user(conn, player_two.id)

                existing_match = await find_open_match_between(conn, player_one.id, player_two.id)
                if existing_match:
                    return await ctx.followup.send(
                        f"{player_one.mention} and {player_two.mention} already have an open battle (match `{existing_match['match_id']}`, status: {existing_match['status']}). "
                        "Only one unresolved battle is allowed between the same two users at a time.",
                        ephemeral=True,
                    )

                player_one_available = spendable(player_one_row)
                player_two_available = spendable(player_two_row)

                if player_one_available < amount:
                    return await ctx.followup.send(
                        f"{player_one.mention} doesn't have enough {CURRENCY_NAME}. They need {fmt(amount)} but only have {fmt(player_one_available)} available.",
                        ephemeral=True,
                    )
                if player_two_available < amount:
                    return await ctx.followup.send(
                        f"{player_two.mention} doesn't have enough {CURRENCY_NAME}. They need {fmt(amount)} but only have {fmt(player_two_available)} available.",
                        ephemeral=True,
                    )

                match_id = gen_id()
                while await conn.fetchrow("SELECT 1 FROM matches WHERE match_id = $1", match_id):
                    match_id = gen_id()

                await debit_escrow(conn, player_one.id, amount, f"forced battle wager escrowed (match {match_id})", fmt_user(player_one))
                await debit_escrow(conn, player_two.id, amount, f"forced battle wager escrowed (match {match_id})", fmt_user(player_two))
                await conn.execute(
                    """
                    INSERT INTO matches (match_id, challenger_id, opponent_id, wager_amount, status, channel_id, created_at, accepted_at)
                    VALUES ($1, $2, $3, $4, 'ACCEPTED', $5, $6, $7)
                    """,
                    match_id,
                    str(player_one.id),
                    str(player_two.id),
                    amount,
                    str(ctx.channel_id),
                    now_utc(),
                    now_utc(),
                )

            embed = discord.Embed(
                title="⚔️ Forced Battle Created!",
                description=f"{player_one.mention} vs {player_two.mention}",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Wager", value=fmt(amount), inline=True)
            embed.add_field(name="Match ID", value=match_id, inline=True)
            embed.add_field(
                name="Status",
                value="Bets are now open! Use `/bet` to wager on a player.\nWhen ready, either player or a moderator can press **Start Match** below or use `/start`.",
                inline=False,
            )
            embed.set_footer(text=f"Force-created by mod: {fmt_user(ctx.author)}")

            view = MatchStartView(match_id, player_one.id, player_two.id)
            msg = await ctx.followup.send(embed=embed, view=view, ephemeral=False, wait=True)
            if msg:
                async with get_db_pool().acquire() as conn:
                    await conn.execute("UPDATE matches SET message_id = $1 WHERE match_id = $2", str(msg.id), match_id)

            await log(
                f"⚔️ FORCED BATTLE CREATED — Mod: {fmt_user(ctx.author)} | {fmt_user(player_one)} vs {fmt_user(player_two)} | Wager: {fmt(amount)} | Match ID: {match_id}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /forcebattle | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Start a battle (locks bets)")
    @option("match_id", str, description="The match ID to start")
    async def start(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                if str(ctx.author.id) not in (match["challenger_id"], match["opponent_id"]) and not has_mod_role(ctx):
                    return await ctx.respond("You must be one of the players or a moderator to start this match.", ephemeral=True)
                if match["status"] != "ACCEPTED":
                    return await ctx.respond(
                        f"Match `{match_id}` is not in ACCEPTED status (current: {match['status']}).",
                        ephemeral=True,
                    )
                await conn.execute(
                    "UPDATE matches SET status = 'ACTIVE', started_at = $1 WHERE match_id = $2",
                    now_utc(),
                    match_id,
                )

            embed = discord.Embed(
                title="🥊 Match Started!",
                description=f"Match `{match_id}` is now **ACTIVE**. Bets are locked.",
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Report Winner",
                value="One of the two players can press the winner button on the match message below when the match is over.",
                inline=False,
            )
            await update_match_message(
                match_id,
                embed,
                view=MatchReportView(match_id, int(match["challenger_id"]), int(match["opponent_id"])),
            )
            await ctx.respond(embed=embed)
            await log(f"🥊 MATCH STARTED — Match ID: {match_id} | Started by: {fmt_user(ctx.author)}")

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /start | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Report the winner of your match")
    @option("match_id", str, description="The match ID")
    @option("winner", discord.Member, description="The winner of the match")
    async def report(ctx: discord.ApplicationContext, match_id: str, winner: discord.Member):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                if str(ctx.author.id) not in (match["challenger_id"], match["opponent_id"]):
                    return await ctx.respond("You are not a participant in this match.", ephemeral=True)
                if match["status"] != "ACTIVE":
                    return await ctx.respond(f"Match `{match_id}` is not ACTIVE (current: {match['status']}).", ephemeral=True)
                if str(winner.id) not in (match["challenger_id"], match["opponent_id"]):
                    return await ctx.respond("The winner must be one of the two players.", ephemeral=True)

                await conn.execute(
                    "UPDATE matches SET status = 'COMPLETED', winner_id = $1, reported_by_id = $2 WHERE match_id = $3",
                    str(winner.id),
                    str(ctx.author.id),
                    match_id,
                )
                await ctx.defer()
                await run_payout(conn, match_id, str(winner.id), ctx.channel)

                complete_embed = discord.Embed(
                    title="⚔️ Match Complete!",
                    description=f"Match `{match_id}` has finished.",
                    color=discord.Color.gold(),
                )
                complete_embed.add_field(name="Winner", value=winner.mention, inline=True)
                complete_embed.add_field(name="Status", value="**COMPLETED**", inline=True)
                await update_match_message(match_id, complete_embed, view=None)

                await ctx.followup.send(f"Match `{match_id}` has been completed!", ephemeral=True)

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /report | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Bet on a player in an ongoing match")
    @option("match_id", str, description="The match ID to bet on")
    @option("player", discord.Member, description="The player you're betting on")
    @option("amount", int, description="Amount to bet")
    async def bet(ctx: discord.ApplicationContext, match_id: str, player: discord.Member, amount: int):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            if amount <= 0:
                return await ctx.respond("Bet amount must be greater than zero.", ephemeral=True)

            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                if str(ctx.author.id) in (match["challenger_id"], match["opponent_id"]):
                    return await ctx.respond("Players cannot bet on their own match.", ephemeral=True)
                if match["status"] != "ACCEPTED":
                    return await ctx.respond(
                        f"Bets are only open during ACCEPTED status (current: {match['status']}).",
                        ephemeral=True,
                    )
                if str(player.id) not in (match["challenger_id"], match["opponent_id"]):
                    return await ctx.respond("You must bet on one of the two players.", ephemeral=True)

                existing = await conn.fetchrow(
                    "SELECT 1 FROM bets WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'",
                    match_id,
                    str(ctx.author.id),
                )
                if existing:
                    return await ctx.respond(
                        "You already have an active bet on this match. Use `/cancelbet` to change it.",
                        ephemeral=True,
                    )

                bettor_row = await get_user(conn, ctx.author.id)
                bettor_available = spendable(bettor_row)
                if bettor_available < amount:
                    return await ctx.respond(f"Insufficient funds. You have {fmt(bettor_available)} available.", ephemeral=True)

                bet_id = gen_id(5)
                while await conn.fetchrow("SELECT 1 FROM bets WHERE bet_id = $1", bet_id):
                    bet_id = gen_id(5)

                await debit_escrow(conn, ctx.author.id, amount, f"bet escrowed (match {match_id})", fmt_user(ctx.author))
                await conn.execute(
                    "INSERT INTO bets (bet_id, match_id, bettor_id, predicted_winner_id, amount, status) VALUES ($1, $2, $3, $4, $5, 'PENDING')",
                    bet_id,
                    match_id,
                    str(ctx.author.id),
                    str(player.id),
                    amount,
                )

            embed = discord.Embed(
                title="🎲 Bet Placed!",
                description=f"{ctx.author.mention} bet {fmt(amount)} on {player.mention} in match `{match_id}`",
                color=discord.Color.purple(),
            )
            await ctx.respond(embed=embed)
            await log(
                f"🎲 BET PLACED — {fmt_user(ctx.author)} bet {fmt(amount)} on {fmt_user(player)} | Match: {match_id} | Bet ID: {bet_id}"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /bet | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description=f"Give some of your {CURRENCY_NAME} to another user")
    @option("user", discord.Member, description="User to give money to")
    @option("amount", int, description=f"Amount of {CURRENCY_NAME} to give")
    async def give(ctx: discord.ApplicationContext, user: discord.Member, amount: int):
        try:
            if not await enforce_channel(ctx):
                return
            if amount <= 0:
                return await ctx.respond(f"You must give more than {fmt(0)}.", ephemeral=True)
            if user.id == ctx.author.id:
                return await ctx.respond("You cannot give money to yourself.", ephemeral=True)
            if user.bot:
                return await ctx.respond("You cannot give money to a bot.", ephemeral=True)

            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                await ensure_user(conn, user.id)

                sender_row = await get_user(conn, ctx.author.id)
                sender_available = spendable(sender_row)
                if sender_available < amount:
                    return await ctx.respond(
                        f"You only have {fmt(sender_available)} available, so you can't give {fmt(amount)}.",
                        ephemeral=True,
                    )

                await conn.execute(
                    "UPDATE users SET balance = balance - $1 WHERE user_id = $2",
                    amount,
                    str(ctx.author.id),
                )
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                    amount,
                    str(user.id),
                )

                sender_updated = await get_user(conn, ctx.author.id)
                recipient_updated = await get_user(conn, user.id)

            embed = discord.Embed(
                title="💸 Transfer Complete",
                description=f"{ctx.author.mention} gave {fmt(amount)} to {user.mention}.",
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name=f"{ctx.author.display_name}'s New Available",
                value=fmt(sender_updated["balance"] - sender_updated["escrow"]),
                inline=True,
            )
            embed.add_field(
                name=f"{user.display_name}'s New Available",
                value=fmt(recipient_updated["balance"] - recipient_updated["escrow"]),
                inline=True,
            )
            await ctx.respond(embed=embed)

            await log(
                f"💸 TRANSFER — {fmt_user(ctx.author)} gave {fmt(amount)} to {fmt_user(user)} | Sender available now: {fmt(sender_updated['balance'] - sender_updated['escrow'])} | Recipient available now: {fmt(recipient_updated['balance'] - recipient_updated['escrow'])}"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /give | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Check your balance or another user's balance")
    @option("user", discord.Member, description="User to check (defaults to you)", required=False)
    async def balance(ctx: discord.ApplicationContext, user: discord.Member = None):
        try:
            if not await enforce_channel(ctx):
                return
            target = user or ctx.author
            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, target.id)
                row = await get_user(conn, target.id)

            avail = row["balance"] - row["escrow"]
            embed = discord.Embed(title=f"💰 {target.display_name}'s Balance", color=discord.Color.green())
            embed.add_field(name="Available", value=fmt(avail), inline=True)
            embed.add_field(name="In Escrow", value=fmt(row["escrow"]), inline=True)
            embed.add_field(name="Total", value=fmt(row["balance"]), inline=True)
            await ctx.respond(embed=embed)

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /balance | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description=f"Claim your daily {fmt(DAILY_AMOUNT)} {CURRENCY_NAME}")
    async def daily(ctx: discord.ApplicationContext):
        try:
            if not await enforce_channel(ctx):
                return
            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                row = await get_user(conn, ctx.author.id)
                now = now_utc()
                if row["last_daily"]:
                    next_claim = row["last_daily"] + timedelta(hours=24)
                    if now < next_claim:
                        remaining = next_claim - now
                        hours, rem = divmod(int(remaining.total_seconds()), 3600)
                        minutes = rem // 60
                        return await ctx.respond(
                            f"You already claimed your daily! Come back in **{hours}h {minutes}m**.",
                            ephemeral=True,
                        )

                await conn.execute(
                    "UPDATE users SET balance = balance + $1, last_daily = $2 WHERE user_id = $3",
                    DAILY_AMOUNT,
                    now,
                    str(ctx.author.id),
                )
                updated = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", str(ctx.author.id))

            embed = discord.Embed(
                title="☀️ Daily Claimed!",
                description=f"You received {fmt(DAILY_AMOUNT)} {CURRENCY_NAME}!\nNew balance: {fmt(updated['balance'])}",
                color=discord.Color.yellow(),
            )
            await ctx.respond(embed=embed)
            await log(
                f"☀️ DAILY CLAIMED — {fmt_user(ctx.author)} received {fmt(DAILY_AMOUNT)} | New balance: {fmt(updated['balance'])}"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /daily | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Show the leaderboard from most money to least")
    @option("page", int, description="Page number", required=False)
    async def top(ctx: discord.ApplicationContext, page: int = 1):
        try:
            if not await enforce_channel(ctx):
                return
            page = max(1, page)
            view = TopLeaderboardView(ctx.author.id, page)
            embed, view.total_pages = await build_top_embed(page)
            view.page = max(1, min(page, view.total_pages))
            await view.refresh_buttons()
            await ctx.respond(embed=embed, view=view)
        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /top | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Cancel a battle you created (before it's accepted)")
    @option("match_id", str, description="The match ID to cancel")
    async def cancelbattle(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                if match["challenger_id"] != str(ctx.author.id):
                    return await ctx.respond("Only the challenger can cancel a battle.", ephemeral=True)
                if match["status"] != "PENDING":
                    return await ctx.respond(
                        f"You can only cancel a PENDING match (current: {match['status']}).",
                        ephemeral=True,
                    )

                await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", match_id)
                await release_escrow(
                    conn,
                    ctx.author.id,
                    match["wager_amount"],
                    f"battle cancelled by challenger (match {match_id})",
                    fmt_user(ctx.author),
                )

                channel = get_bot().get_channel(int(match["channel_id"]))
                if channel and match["message_id"]:
                    try:
                        msg = await channel.fetch_message(int(match["message_id"]))
                        embed = discord.Embed(
                            title="❌ Battle Cancelled",
                            description=f"{ctx.author.mention} cancelled the challenge. Wager refunded.",
                            color=discord.Color.dark_gray(),
                        )
                        await msg.edit(embed=embed, view=None)
                    except Exception:
                        pass

            await ctx.respond(
                f"Battle `{match_id}` cancelled and your wager of {fmt(match['wager_amount'])} has been refunded.",
                ephemeral=True,
            )
            await log(
                f"🚫 BATTLE CANCELLED — Match ID: {match_id} | Cancelled by: {fmt_user(ctx.author)} | Wager refunded: {fmt(match['wager_amount'])}"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /cancelbattle | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Accept a pending battle for the users")
    @option("match_id", str, description="The match ID to accept")
    async def forceaccept(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            match_id = match_id.upper()
            await ctx.defer(ephemeral=True)

            async with get_db_pool().acquire() as conn:
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                if match["status"] != "PENDING":
                    return await ctx.followup.send(
                        f"Only PENDING matches can be force-accepted (current: {match['status']}).",
                        ephemeral=True,
                    )

                opponent_id = int(match["opponent_id"])
                await ensure_user(conn, opponent_id)
                opponent_row = await get_user(conn, opponent_id)
                opponent_available = spendable(opponent_row)
                if opponent_available < match["wager_amount"]:
                    opponent_user = await get_bot().fetch_user(opponent_id)
                    return await ctx.followup.send(
                        f"{opponent_user.mention} doesn't have enough {CURRENCY_NAME}. They need {fmt(match['wager_amount'])} but only have {fmt(opponent_available)} available.",
                        ephemeral=True,
                    )

                opponent_user = await get_bot().fetch_user(opponent_id)
                challenger_user = await get_bot().fetch_user(int(match["challenger_id"]))
                await debit_escrow(
                    conn,
                    opponent_id,
                    match["wager_amount"],
                    f"battle wager escrowed by mod accept (match {match_id})",
                    fmt_user(opponent_user),
                )
                await conn.execute(
                    "UPDATE matches SET status = 'ACCEPTED', accepted_at = $1 WHERE match_id = $2",
                    now_utc(),
                    match_id,
                )

            embed = await build_accepted_match_embed(
                match_id,
                int(match["challenger_id"]),
                int(match["opponent_id"]),
                match["wager_amount"],
                accepted_by_text=f"Force-accepted by mod: {fmt_user(ctx.author)}",
            )
            await update_match_message(
                match_id,
                embed,
                view=MatchStartView(match_id, int(match["challenger_id"]), int(match["opponent_id"])),
            )

            await ctx.followup.send(f"Match `{match_id}` has been force-accepted.", ephemeral=True)
            await log(
                f"✅ BATTLE FORCE-ACCEPTED — Match ID: {match_id} | Mod: {fmt_user(ctx.author)} | {fmt_user(challenger_user)} vs {fmt_user(opponent_user)} | Wager: {fmt(match['wager_amount'])}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /forceaccept | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Cancel a pending battle for the users")
    @option("match_id", str, description="The match ID to cancel")
    async def forcecancel(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            match_id = match_id.upper()
            await ctx.defer(ephemeral=True)

            async with get_db_pool().acquire() as conn:
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                if match["status"] != "PENDING":
                    return await ctx.followup.send(
                        f"Only PENDING matches can be force-cancelled (current: {match['status']}).",
                        ephemeral=True,
                    )

                challenger_id = int(match["challenger_id"])
                challenger_user = await get_bot().fetch_user(challenger_id)
                await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", match_id)
                await release_escrow(
                    conn,
                    challenger_id,
                    match["wager_amount"],
                    f"battle force-cancelled by mod (match {match_id})",
                    fmt_user(challenger_user),
                )

            embed = await build_cancelled_match_embed(
                f"A moderator cancelled this challenge. {challenger_user.mention}'s wager has been refunded."
            )
            await update_match_message(match_id, embed, view=None)

            await ctx.followup.send(f"Match `{match_id}` has been force-cancelled.", ephemeral=True)
            await log(
                f"🚫 BATTLE FORCE-CANCELLED — Match ID: {match_id} | Mod: {fmt_user(ctx.author)} | Challenger refunded: {fmt_user(challenger_user)} | Wager: {fmt(match['wager_amount'])}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /forcecancel | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Cancel your bet on a match (before it starts)")
    @option("match_id", str, description="The match ID your bet is on")
    async def cancelbet(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, ctx.author.id)
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                if match["status"] != "ACCEPTED":
                    return await ctx.respond(
                        f"Bets can only be cancelled while match is ACCEPTED (current: {match['status']}).",
                        ephemeral=True,
                    )

                existing_bet = await conn.fetchrow(
                    "SELECT * FROM bets WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'",
                    match_id,
                    str(ctx.author.id),
                )
                if not existing_bet:
                    return await ctx.respond(f"You don't have an active bet on match `{match_id}`.", ephemeral=True)

                await release_escrow(
                    conn,
                    ctx.author.id,
                    existing_bet["amount"],
                    f"bet cancelled (match {match_id})",
                    fmt_user(ctx.author),
                )
                await conn.execute("DELETE FROM bets WHERE bet_id = $1", existing_bet["bet_id"])

            await ctx.respond(
                f"Your bet of {fmt(existing_bet['amount'])} on match `{match_id}` has been cancelled and refunded.",
                ephemeral=True,
            )
            await log(
                f"🚫 BET CANCELLED — {fmt_user(ctx.author)} cancelled bet of {fmt(existing_bet['amount'])} on match {match_id} | Refunded"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /cancelbet | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Reset a user's balance, available balance, and escrow to 0")
    @option("user", discord.Member, description="User to reset")
    async def reset(ctx: discord.ApplicationContext, user: discord.Member):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, user.id)
                row = await get_user(conn, user.id)
                old_balance = row["balance"]
                old_escrow = row["escrow"]
                old_available = old_balance - old_escrow

                await conn.execute("UPDATE users SET balance = 0, escrow = 0 WHERE user_id = $1", str(user.id))

            await ctx.respond(
                f"Reset {user.mention}'s balance. Previous totals — Available: {fmt(old_available)} | In Escrow: {fmt(old_escrow)} | Total: {fmt(old_balance)}. New totals are all {fmt(0)}.",
                ephemeral=True,
            )
            await log(
                f"🧹 BALANCE RESET — Mod: {fmt_user(ctx.author)} | User: {fmt_user(user)} | Old available: {fmt(old_available)} | Old escrow: {fmt(old_escrow)} | Old total: {fmt(old_balance)} | New available: {fmt(0)} | New escrow: {fmt(0)} | New total: {fmt(0)}"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /reset | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Adjust a user's balance")
    @option("user", discord.Member, description="User to adjust")
    @option("amount", int, description="Amount to add (positive) or remove (negative)")
    async def adjustbalance(ctx: discord.ApplicationContext, user: discord.Member, amount: int):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            async with get_db_pool().acquire() as conn:
                await ensure_user(conn, user.id)
                row = await get_user(conn, user.id)
                new_balance = max(0, row["balance"] + amount)
                actual_change = new_balance - row["balance"]
                await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, str(user.id))

            note = ""
            if actual_change != amount:
                note = f" (capped at 0; requested {fmt(amount)})"
            await ctx.respond(
                f"Adjusted {user.mention}'s balance by {fmt(actual_change)}{note}. New balance: {fmt(new_balance)}",
                ephemeral=True,
            )
            await log(
                f"🔧 BALANCE ADJUSTED — Mod: {fmt_user(ctx.author)} | User: {fmt_user(user)} | Change: {fmt(actual_change)}{note} | New balance: {fmt(new_balance)}"
            )

        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /adjustbalance | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Force-resolve a match")
    @option("match_id", str, description="The match ID to resolve")
    @option("winner", discord.Member, description="Declare the winner")
    async def resolve(ctx: discord.ApplicationContext, match_id: str, winner: discord.Member):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            match_id = match_id.upper()
            await ctx.defer(ephemeral=True)

            async with get_db_pool().acquire() as conn:
                match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
                if not match:
                    return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                if str(winner.id) not in (match["challenger_id"], match["opponent_id"]):
                    return await ctx.followup.send(
                        "The winner must be one of the two players in this match.",
                        ephemeral=True,
                    )
                if match["status"] not in ("ACCEPTED", "ACTIVE", "COMPLETED"):
                    return await ctx.followup.send(
                        f"Match `{match_id}` cannot be force-resolved from `{match['status']}`. Only ACCEPTED, ACTIVE, or COMPLETED matches can be resolved.",
                        ephemeral=True,
                    )

                if match["status"] == "COMPLETED":
                    if match["winner_id"] == str(winner.id):
                        return await ctx.followup.send(
                            f"Match `{match_id}` is already completed with {winner.mention} as the winner. No money was changed.",
                            ephemeral=True,
                        )

                    await restore_match_to_pre_payout(conn, match_id, match["winner_id"])

                await conn.execute(
                    "UPDATE matches SET status = 'COMPLETED', winner_id = $1, reported_by_id = $2 WHERE match_id = $3",
                    str(winner.id),
                    str(ctx.author.id),
                    match_id,
                )
                await run_payout(conn, match_id, str(winner.id), ctx.channel, mod_tag=fmt_user(ctx.author))

            resolve_embed = discord.Embed(
                title="⚔️ Match Complete!",
                description=f"Match `{match_id}` was force-resolved by a moderator.",
                color=discord.Color.gold(),
            )
            resolve_embed.add_field(name="Winner", value=winner.mention, inline=True)
            resolve_embed.add_field(name="Status", value="**COMPLETED**", inline=True)
            await update_match_message(match_id, resolve_embed, view=None)

            await ctx.followup.send(f"Match `{match_id}` has been force-resolved.", ephemeral=True)
            await log(
                f"🔧 MATCH FORCE-RESOLVED — Match ID: {match_id} | Mod: {fmt_user(ctx.author)} | Winner: {fmt_user(winner)}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /resolve | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")