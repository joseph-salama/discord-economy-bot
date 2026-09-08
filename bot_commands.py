from datetime import timedelta
import asyncio
import traceback

import asyncpg
import discord
from discord import option

from bot_commands_admin import register_admin_commands
from bot_helpers import (
    CURRENCY_NAME,
    DAILY_AMOUNT,
    MIN_BATTLE_WAGER,
    InsufficientFundsError,
    build_open_matches_embed,
    build_top_embed,
    cancel_match_if_pending,
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
    lock_match,
    lock_user,
    lock_users_ordered,
    log,
    now_utc,
    propose_or_confirm_winner,
    release_escrow,
    release_escrow_up_to,
    run_payout,
    spendable,
    update_match_message,
)
from bot_views import (
    ChallengeView,
    MatchReportView,
    MatchStartView,
    TopLeaderboardView,
    cancel_accepted_expiry_task,
    cancel_all_player_match_expiry_tasks,
    cancel_challenge_expiry_task,
    schedule_active_expiry,
    schedule_accepted_expiry,
    schedule_challenge_expiry,
)


def register_commands(bot: discord.Bot):
    register_admin_commands(bot)

    @bot.slash_command(description="Show all commands available to you")
    async def help(ctx: discord.ApplicationContext):
        try:
            if not await enforce_channel(ctx):
                return

            commands_list = [
                "**/battle** — Challenge another player to a battle.",
                "**/start** — Start an accepted match.",
                "**/report** — Propose/confirm the winner (both players must agree).",
                "**/bet** — Bet on a player in an accepted match (pari-mutuel pool).",
                "**/balance** — Check your balance or another user's balance.",
                f"**/give** — Give some of your {CURRENCY_NAME} to another user.",
                f"**/daily** — Claim your daily {fmt(DAILY_AMOUNT)} {CURRENCY_NAME}.",
                "**/top** — View the money leaderboard.",
                "**/openmatches** — List all open battles.",
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
                    "**/cancelactives** — [MOD] Cancel all open player and team matches and refund wagers/bets.",
                    "**/match** — [MOD] Create a team (role) match people can bet on.",
                    "**/cancelteam** — [MOD] Cancel an OPEN/ACTIVE team match and refund bets.",
                    "**/resolveteam** — [MOD] Force-resolve a team match (can reverse a wrong winner).",
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

            match_created = False
            match_id = None
            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        await ensure_user(conn, ctx.author.id)
                        await ensure_user(conn, opponent.id)
                        locked = await lock_users_ordered(conn, ctx.author.id, opponent.id)
                        challenger_row = locked.get(ctx.author.id)
                        opponent_row = locked.get(opponent.id)
                        if not challenger_row or not opponent_row:
                            return await ctx.followup.send(
                                "Could not load balances. Please try again.",
                                ephemeral=True,
                            )

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

                        await debit_escrow(
                            conn,
                            ctx.author.id,
                            amount,
                            f"battle wager escrowed (match {match_id})",
                            fmt_user(ctx.author),
                        )
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
                        match_created = True
            except InsufficientFundsError:
                return await ctx.followup.send(
                    f"You have insufficient funds to create this battle for {fmt(amount)}.",
                    ephemeral=True,
                )

            embed = discord.Embed(title="⚔️ Battle Challenge!", color=discord.Color.orange())
            embed.add_field(name="Challenger", value=ctx.author.mention, inline=True)
            embed.add_field(name="Opponent", value=opponent.mention, inline=True)
            embed.add_field(name="Wager", value=fmt(amount), inline=True)
            embed.add_field(name="Match ID", value=match_id, inline=True)
            from bot_helpers import CHALLENGE_TIMEOUT_SECONDS
            embed.set_footer(text=f"Challenge expires in {CHALLENGE_TIMEOUT_SECONDS // 60} minutes")

            view = ChallengeView(match_id, ctx.author.id, opponent.id)
            get_bot().add_view(view)
            schedule_challenge_expiry(match_id)
            try:
                msg = await ctx.followup.send(embed=embed, view=view, wait=True)
                if not msg:
                    raise RuntimeError("Failed to post challenge message")
                async with get_db_pool().acquire() as conn:
                    for _ in range(3):
                        try:
                            await conn.execute(
                                "UPDATE matches SET message_id = $1 WHERE match_id = $2",
                                str(msg.id),
                                match_id,
                            )
                            break
                        except Exception:
                            await asyncio.sleep(0.25)
                    else:
                        await log(
                            f"⚠️ MESSAGE ID SAVE FAILED — Match ID: {match_id} | Message was posted but message_id was not saved"
                        )
            except Exception:
                if match_created and match_id:
                    cancel_challenge_expiry_task(match_id)
                    async with get_db_pool().acquire() as conn:
                        async with conn.transaction():
                            cancelled = await cancel_match_if_pending(conn, match_id)
                            if cancelled:
                                await release_escrow_up_to(
                                    conn,
                                    ctx.author.id,
                                    amount,
                                    f"battle create rollback (match {match_id})",
                                    fmt_user(ctx.author),
                                )
                    await ctx.followup.send(
                        "Could not post the battle message, so the challenge was cancelled and your wager was refunded.",
                        ephemeral=True,
                    )
                    return
                raise

            await log(
                f"⚔️ BATTLE CREATED — Challenger: {fmt_user(ctx.author)} vs Opponent: {fmt_user(opponent)} | Wager: {fmt(amount)} | Match ID: {match_id}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong. Please try again.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong. Please try again.", ephemeral=True)
            await log(f"❌ ERROR — Command: /battle | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Start a battle (locks bets)")
    @option("match_id", str, description="The match ID to start")
    async def start(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            await ctx.defer()
            match_id = match_id.upper()
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    await ensure_user(conn, ctx.author.id)
                    match = await lock_match(conn, match_id)
                    if not match:
                        return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                    if str(ctx.author.id) not in (match["challenger_id"], match["opponent_id"]) and not has_mod_role(ctx):
                        return await ctx.followup.send(
                            "You must be one of the players or a moderator to start this match.",
                            ephemeral=True,
                        )
                    if match["status"] != "ACCEPTED":
                        return await ctx.followup.send(
                            f"Match `{match_id}` is not in ACCEPTED status (current: {match['status']}).",
                            ephemeral=True,
                        )
                    started = await conn.fetchrow(
                        """
                        UPDATE matches
                        SET status = 'ACTIVE', started_at = $1
                        WHERE match_id = $2 AND status = 'ACCEPTED'
                        RETURNING *
                        """,
                        now_utc(),
                        match_id,
                    )
                    if not started:
                        return await ctx.followup.send(
                            f"Match `{match_id}` could not be started (it may have just changed status).",
                            ephemeral=True,
                        )

            cancel_accepted_expiry_task(match_id)
            schedule_active_expiry(match_id)
            embed = discord.Embed(
                title="🥊 Match Started!",
                description=f"Match `{match_id}` is now **ACTIVE**. Bets are locked.",
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Report Winner",
                value="Both players must report the **same** winner (buttons or `/report`). A moderator can still `/resolve`.",
                inline=False,
            )
            report_view = await MatchReportView.create(match_id, int(match["challenger_id"]), int(match["opponent_id"]))
            get_bot().add_view(report_view)
            await update_match_message(match_id, embed, view=report_view)
            await ctx.followup.send(embed=embed)
            await log(f"🥊 MATCH STARTED — Match ID: {match_id} | Started by: {fmt_user(ctx.author)}")

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /start | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Propose or confirm the winner of your match (both players must agree)")
    @option("match_id", str, description="The match ID")
    @option("winner", discord.Member, description="The winner of the match")
    async def report(ctx: discord.ApplicationContext, match_id: str, winner: discord.Member):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            await ctx.defer()
            payout_embed = None
            result = None
            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        await ensure_user(conn, ctx.author.id)
                        try:
                            result, completed = await propose_or_confirm_winner(
                                conn,
                                match_id,
                                str(winner.id),
                                str(ctx.author.id),
                            )
                        except ValueError as e:
                            code = str(e)
                            if code == "missing_match":
                                return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                            if code == "bad_reporter":
                                return await ctx.followup.send(
                                    "You are not a participant in this match.",
                                    ephemeral=True,
                                )
                            if code.startswith("bad_status:"):
                                status = code.split(":", 1)[1]
                                return await ctx.followup.send(
                                    f"Match `{match_id}` is not ACTIVE (current: {status}).",
                                    ephemeral=True,
                                )
                            if code == "bad_winner":
                                return await ctx.followup.send(
                                    "The winner must be one of the two players.",
                                    ephemeral=True,
                                )
                            if code == "race_completed":
                                return await ctx.followup.send(
                                    f"Match `{match_id}` was already completed by someone else.",
                                    ephemeral=True,
                                )
                            return await ctx.followup.send("Could not process that winner report.", ephemeral=True)

                        if result == "confirmed" and completed:
                            payout_embed = await run_payout(conn, match_id, str(winner.id))
            except InsufficientFundsError as e:
                await log(f"❌ ERROR — /report payout failed for {match_id}: {e}")
                return await ctx.followup.send(
                    "Payout failed because escrowed funds were inconsistent. Please ask a moderator to use `/resolve`.",
                    ephemeral=True,
                )

            if result != "confirmed":
                if result == "disputed":
                    note = (
                        f"You proposed a different winner ({winner.mention}). "
                        "Waiting for the other player to confirm the same winner, or a mod `/resolve`."
                    )
                elif result == "waiting":
                    note = f"You already proposed {winner.mention}. Waiting for the other player to confirm."
                elif result == "updated":
                    note = f"Updated your proposal to {winner.mention}. Waiting for the other player to confirm."
                else:
                    note = (
                        f"Proposed {winner.mention} as the winner. "
                        "Waiting for the other player to confirm the same result."
                    )

                pending_embed = discord.Embed(
                    title="🥊 Match Report Pending",
                    description=note,
                    color=discord.Color.orange(),
                )
                pending_embed.add_field(name="Match ID", value=match_id, inline=True)
                pending_embed.add_field(name="Proposed Winner", value=winner.mention, inline=True)
                pending_embed.add_field(name="Status", value="**ACTIVE** — awaiting confirmation", inline=True)
                async with get_db_pool().acquire() as conn:
                    match = await conn.fetchrow(
                        "SELECT challenger_id, opponent_id FROM matches WHERE match_id = $1",
                        match_id,
                    )
                if match:
                    view = await MatchReportView.create(
                        match_id,
                        int(match["challenger_id"]),
                        int(match["opponent_id"]),
                    )
                    get_bot().add_view(view)
                    await update_match_message(match_id, pending_embed, view=view)
                await ctx.followup.send(note)
                await log(
                    f"📝 MATCH REPORT PROPOSED — Match ID: {match_id} | By: {fmt_user(ctx.author)} | "
                    f"Winner: {fmt_user(winner)} | Result: {result}"
                )
                return

            if payout_embed:
                await ctx.channel.send(embed=payout_embed)

            cancel_all_player_match_expiry_tasks(match_id)
            complete_embed = discord.Embed(
                title="⚔️ Match Complete!",
                description=f"Match `{match_id}` has finished.",
                color=discord.Color.gold(),
            )
            complete_embed.add_field(name="Winner", value=winner.mention, inline=True)
            complete_embed.add_field(name="Status", value="**COMPLETED**", inline=True)
            await update_match_message(match_id, complete_embed, view=None)

            await ctx.followup.send(f"Match `{match_id}` has been completed!")

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

            await ctx.defer(ephemeral=True)
            async with get_db_pool().acquire() as conn:
                try:
                    async with conn.transaction():
                        await ensure_user(conn, ctx.author.id)
                        match = await lock_match(conn, match_id)
                        if not match:
                            return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                        if str(ctx.author.id) in (match["challenger_id"], match["opponent_id"]):
                            return await ctx.followup.send("Players cannot bet on their own match.", ephemeral=True)
                        if match["status"] != "ACCEPTED":
                            return await ctx.followup.send(
                                f"Bets are only open during ACCEPTED status (current: {match['status']}).",
                                ephemeral=True,
                            )
                        if str(player.id) not in (match["challenger_id"], match["opponent_id"]):
                            return await ctx.followup.send("You must bet on one of the two players.", ephemeral=True)

                        existing = await conn.fetchrow(
                            "SELECT 1 FROM bets WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'",
                            match_id,
                            str(ctx.author.id),
                        )
                        if existing:
                            return await ctx.followup.send(
                                "You already have an active bet on this match. Use `/cancelbet` to change it.",
                                ephemeral=True,
                            )

                        bettor_row = await lock_user(conn, ctx.author.id)
                        bettor_available = spendable(bettor_row)
                        if bettor_available < amount:
                            return await ctx.followup.send(
                                f"Insufficient funds. You have {fmt(bettor_available)} available.",
                                ephemeral=True,
                            )

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
                except asyncpg.UniqueViolationError:
                    return await ctx.followup.send(
                        "You already have an active bet on this match. Use `/cancelbet` to change it.",
                        ephemeral=True,
                    )
                except InsufficientFundsError:
                    return await ctx.followup.send(f"Insufficient funds. You can't bet {fmt(amount)}.", ephemeral=True)

            embed = discord.Embed(
                title="🎲 Bet Placed!",
                description=f"{ctx.author.mention} bet {fmt(amount)} on {player.mention} in match `{match_id}`",
                color=discord.Color.purple(),
            )
            await ctx.followup.send(embed=embed)
            await log(
                f"🎲 BET PLACED — {fmt_user(ctx.author)} bet {fmt(amount)} on {fmt_user(player)} | Match: {match_id} | Bet ID: {bet_id}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
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
                async with conn.transaction():
                    await ensure_user(conn, ctx.author.id)
                    await ensure_user(conn, user.id)

                    locked = await lock_users_ordered(conn, ctx.author.id, user.id)
                    sender_row = locked.get(ctx.author.id)
                    if not sender_row or locked.get(user.id) is None:
                        return await ctx.respond("Could not load balances. Please try again.", ephemeral=True)
                    sender_available = spendable(sender_row)
                    if sender_available < amount:
                        return await ctx.respond(
                            f"You only have {fmt(sender_available)} available, so you can't give {fmt(amount)}.",
                            ephemeral=True,
                        )

                    sender_updated = await conn.fetchrow(
                        """
                        UPDATE users
                        SET balance = balance - $1
                        WHERE user_id = $2 AND balance - escrow >= $1
                        RETURNING balance, escrow
                        """,
                        amount,
                        str(ctx.author.id),
                    )
                    if not sender_updated:
                        return await ctx.respond(
                            f"You only have {fmt(sender_available)} available, so you can't give {fmt(amount)}.",
                            ephemeral=True,
                        )

                    await conn.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                        amount,
                        str(user.id),
                    )
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
                async with conn.transaction():
                    await ensure_user(conn, ctx.author.id)
                    row = await lock_user(conn, ctx.author.id)
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

                    updated = await conn.fetchrow(
                        """
                        UPDATE users
                        SET balance = balance + $1, last_daily = $2
                        WHERE user_id = $3
                          AND (last_daily IS NULL OR last_daily <= $4)
                        RETURNING balance
                        """,
                        DAILY_AMOUNT,
                        now,
                        str(ctx.author.id),
                        now - timedelta(hours=24),
                    )
                    if not updated:
                        return await ctx.respond(
                            "You already claimed your daily! Please try again in a little while.",
                            ephemeral=True,
                        )

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
            view.message = await ctx.interaction.original_response()
        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /top | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="List all open battles (pending, accepted, and active)")
    @option("page", int, description="Page number", required=False)
    async def openmatches(ctx: discord.ApplicationContext, page: int = 1):
        try:
            if not await enforce_channel(ctx):
                return
            page = max(1, page)
            embed, _total_pages = await build_open_matches_embed(page)
            await ctx.respond(embed=embed)
        except Exception:
            await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /openmatches | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="Cancel a battle you created (before it's accepted)")
    @option("match_id", str, description="The match ID to cancel")
    async def cancelbattle(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        await ensure_user(conn, ctx.author.id)
                        match = await lock_match(conn, match_id)
                        if not match:
                            return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                        if match["challenger_id"] != str(ctx.author.id):
                            return await ctx.respond("Only the challenger can cancel a battle.", ephemeral=True)
                        if match["status"] != "PENDING":
                            return await ctx.respond(
                                f"You can only cancel a PENDING match (current: {match['status']}).",
                                ephemeral=True,
                            )

                        cancelled = await cancel_match_if_pending(conn, match_id)
                        if not cancelled:
                            return await ctx.respond(
                                f"Match `{match_id}` could not be cancelled (it may have just changed status).",
                                ephemeral=True,
                            )
                        await release_escrow(
                            conn,
                            ctx.author.id,
                            match["wager_amount"],
                            f"battle cancelled by challenger (match {match_id})",
                            fmt_user(ctx.author),
                        )
            except InsufficientFundsError:
                return await ctx.respond(
                    "Could not refund this battle cleanly. Please ask a moderator for help.",
                    ephemeral=True,
                )

            cancel_all_player_match_expiry_tasks(match_id)

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

    @bot.slash_command(description="Cancel your bet on a match (before it starts)")
    @option("match_id", str, description="The match ID your bet is on")
    async def cancelbet(ctx: discord.ApplicationContext, match_id: str):
        try:
            if not await enforce_channel(ctx):
                return
            match_id = match_id.upper()
            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        await ensure_user(conn, ctx.author.id)
                        match = await lock_match(conn, match_id)
                        if not match:
                            return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
                        if match["status"] != "ACCEPTED":
                            return await ctx.respond(
                                f"Bets can only be cancelled while match is ACCEPTED (current: {match['status']}).",
                                ephemeral=True,
                            )

                        existing_bet = await conn.fetchrow(
                            """
                            SELECT * FROM bets
                            WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'
                            FOR UPDATE
                            """,
                            match_id,
                            str(ctx.author.id),
                        )
                        if not existing_bet:
                            return await ctx.respond(f"You don't have an active bet on match `{match_id}`.", ephemeral=True)

                        deleted = await conn.fetchrow(
                            """
                            DELETE FROM bets
                            WHERE bet_id = $1 AND status = 'PENDING'
                            RETURNING *
                            """,
                            existing_bet["bet_id"],
                        )
                        if not deleted:
                            return await ctx.respond(f"You don't have an active bet on match `{match_id}`.", ephemeral=True)

                        await release_escrow(
                            conn,
                            ctx.author.id,
                            existing_bet["amount"],
                            f"bet cancelled (match {match_id})",
                            fmt_user(ctx.author),
                        )
            except InsufficientFundsError:
                return await ctx.respond(
                    "Could not refund this bet cleanly. Please ask a moderator for help.",
                    ephemeral=True,
                )

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