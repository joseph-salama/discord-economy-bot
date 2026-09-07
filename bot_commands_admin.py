import traceback

import discord
from discord import option

from bot_helpers import (
    CURRENCY_NAME,
    MIN_BATTLE_WAGER,
    InsufficientFundsError,
    PayoutReverseError,
    build_accepted_match_embed,
    build_cancelled_match_embed,
    build_team_match_embed,
    cancel_match_if_pending,
    cancel_open_match_with_refunds,
    debit_escrow,
    enforce_channel,
    ensure_user,
    find_open_match_between,
    fmt,
    fmt_user,
    gen_id,
    get_bot,
    get_db_pool,
    has_mod_role,
    lock_match,
    lock_user,
    log,
    now_utc,
    release_escrow,
    restore_match_to_pre_payout,
    run_payout,
    spendable,
    update_match_message,
)
from bot_views import MatchStartView, cancel_challenge_expiry_task
from bot_team_matches import TeamMatchOpenView


def register_admin_commands(bot: discord.Bot):
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

            await ctx.defer()

            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        await ensure_user(conn, player_one.id)
                        await ensure_user(conn, player_two.id)
                        player_one_row = await lock_user(conn, player_one.id)
                        player_two_row = await lock_user(conn, player_two.id)
                        if not player_one_row or not player_two_row:
                            return await ctx.followup.send(
                                "Could not load one of the players' balances. Please try again.",
                                ephemeral=True,
                            )

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
            except InsufficientFundsError:
                return await ctx.followup.send(
                    "One of the players no longer has enough available funds for this battle.",
                    ephemeral=True,
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
            get_bot().add_view(view)
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

            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        match = await lock_match(conn, match_id)
                        if not match:
                            return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                        if match["status"] != "PENDING":
                            return await ctx.followup.send(
                                f"Only PENDING matches can be force-accepted (current: {match['status']}).",
                                ephemeral=True,
                            )

                        opponent_id = int(match["opponent_id"])
                        await ensure_user(conn, opponent_id)
                        opponent_row = await lock_user(conn, opponent_id)
                        opponent_available = spendable(opponent_row)
                        if opponent_available < match["wager_amount"]:
                            opponent_user = await get_bot().fetch_user(opponent_id)
                            return await ctx.followup.send(
                                f"{opponent_user.mention} doesn't have enough {CURRENCY_NAME}. They need {fmt(match['wager_amount'])} but only have {fmt(opponent_available)} available.",
                                ephemeral=True,
                            )

                        opponent_user = await get_bot().fetch_user(opponent_id)
                        challenger_user = await get_bot().fetch_user(int(match["challenger_id"]))
                        accepted = await conn.fetchrow(
                            """
                            UPDATE matches
                            SET status = 'ACCEPTED', accepted_at = $1
                            WHERE match_id = $2 AND status = 'PENDING'
                            RETURNING *
                            """,
                            now_utc(),
                            match_id,
                        )
                        if not accepted:
                            return await ctx.followup.send(
                                f"Match `{match_id}` could not be force-accepted (it may have just changed status).",
                                ephemeral=True,
                            )
                        await debit_escrow(
                            conn,
                            opponent_id,
                            match["wager_amount"],
                            f"battle wager escrowed by mod accept (match {match_id})",
                            fmt_user(opponent_user),
                        )
            except InsufficientFundsError:
                return await ctx.followup.send(
                    "The opponent no longer has enough available funds to accept this battle.",
                    ephemeral=True,
                )

            cancel_challenge_expiry_task(match_id)
            embed = await build_accepted_match_embed(
                match_id,
                int(match["challenger_id"]),
                int(match["opponent_id"]),
                match["wager_amount"],
                accepted_by_text=f"Force-accepted by mod: {fmt_user(ctx.author)}",
            )
            start_view = MatchStartView(match_id, int(match["challenger_id"]), int(match["opponent_id"]))
            get_bot().add_view(start_view)
            await update_match_message(match_id, embed, view=start_view)

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

            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        match = await lock_match(conn, match_id)
                        if not match:
                            return await ctx.followup.send(f"Match `{match_id}` not found.", ephemeral=True)
                        if match["status"] != "PENDING":
                            return await ctx.followup.send(
                                f"Only PENDING matches can be force-cancelled (current: {match['status']}).",
                                ephemeral=True,
                            )

                        challenger_id = int(match["challenger_id"])
                        challenger_user = await get_bot().fetch_user(challenger_id)
                        cancelled = await cancel_match_if_pending(conn, match_id)
                        if not cancelled:
                            return await ctx.followup.send(
                                f"Match `{match_id}` could not be force-cancelled (it may have just changed status).",
                                ephemeral=True,
                            )
                        await release_escrow(
                            conn,
                            challenger_id,
                            match["wager_amount"],
                            f"battle force-cancelled by mod (match {match_id})",
                            fmt_user(challenger_user),
                        )
            except InsufficientFundsError:
                return await ctx.followup.send(
                    "Could not refund this battle cleanly because escrow was inconsistent.",
                    ephemeral=True,
                )

            cancel_challenge_expiry_task(match_id)
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

    @bot.slash_command(description="[MOD] Reset a user's balance, available balance, and escrow to 0")
    @option("user", discord.Member, description="User to reset")
    async def reset(ctx: discord.ApplicationContext, user: discord.Member):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    await ensure_user(conn, user.id)
                    row = await lock_user(conn, user.id)
                    old_balance = row["balance"]
                    old_escrow = row["escrow"]
                    old_available = old_balance - old_escrow

                    if old_escrow > 0:
                        return await ctx.respond(
                            f"Cannot reset {user.mention} while they have {fmt(old_escrow)} in escrow. "
                            "Cancel/resolve their open battles and bets first.",
                            ephemeral=True,
                        )

                    open_match = await conn.fetchrow(
                        """
                        SELECT match_id, status FROM matches
                        WHERE (challenger_id = $1 OR opponent_id = $1)
                          AND status IN ('PENDING', 'ACCEPTED', 'ACTIVE')
                        LIMIT 1
                        """,
                        str(user.id),
                    )
                    if open_match:
                        return await ctx.respond(
                            f"Cannot reset {user.mention} while they have an open match "
                            f"`{open_match['match_id']}` ({open_match['status']}).",
                            ephemeral=True,
                        )

                    open_bet = await conn.fetchrow(
                        """
                        SELECT bet_id, match_id FROM bets
                        WHERE bettor_id = $1 AND status = 'PENDING'
                        LIMIT 1
                        """,
                        str(user.id),
                    )
                    if open_bet:
                        return await ctx.respond(
                            f"Cannot reset {user.mention} while they have a pending bet "
                            f"`{open_bet['bet_id']}` on match `{open_bet['match_id']}`.",
                            ephemeral=True,
                        )

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
                async with conn.transaction():
                    await ensure_user(conn, user.id)
                    row = await lock_user(conn, user.id)
                    # Never let total balance drop below escrow, or spendable goes negative.
                    floor = max(0, row["escrow"])
                    new_balance = max(floor, row["balance"] + amount)
                    actual_change = new_balance - row["balance"]
                    await conn.execute("UPDATE users SET balance = $1 WHERE user_id = $2", new_balance, str(user.id))

            note = ""
            if actual_change != amount:
                if amount < 0 and floor > 0:
                    note = f" (capped at escrow floor {fmt(floor)}; requested {fmt(amount)})"
                else:
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

            try:
                payout_embed = None
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        match = await lock_match(conn, match_id)
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
                        else:
                            completed = await conn.fetchrow(
                                """
                                UPDATE matches
                                SET status = 'COMPLETED', winner_id = $1, reported_by_id = $2
                                WHERE match_id = $3 AND status IN ('ACCEPTED', 'ACTIVE')
                                RETURNING *
                                """,
                                str(winner.id),
                                str(ctx.author.id),
                                match_id,
                            )
                            if not completed:
                                return await ctx.followup.send(
                                    f"Match `{match_id}` could not be force-resolved (it may have just changed status).",
                                    ephemeral=True,
                                )

                        payout_embed = await run_payout(conn, match_id, str(winner.id), mod_tag=fmt_user(ctx.author))
            except PayoutReverseError as e:
                return await ctx.followup.send(str(e), ephemeral=True)
            except InsufficientFundsError as e:
                await log(f"❌ ERROR — /resolve payout failed for {match_id}: {e}")
                return await ctx.followup.send(
                    "Force-resolve failed because escrowed funds were inconsistent. No money was changed.",
                    ephemeral=True,
                )

            if payout_embed:
                await ctx.channel.send(embed=payout_embed)

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

    @bot.slash_command(description="[MOD] Cancel all open matches and refund wagers/bets")
    async def cancelactives(ctx: discord.ApplicationContext):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

            await ctx.defer(ephemeral=True)

            cancelled_matches = []
            total_player_refunds = 0
            total_bet_refunds = 0

            try:
                async with get_db_pool().acquire() as conn:
                    async with conn.transaction():
                        matches = await conn.fetch(
                            """
                            SELECT *
                            FROM matches
                            WHERE status IN ('PENDING', 'ACCEPTED', 'ACTIVE')
                            ORDER BY created_at ASC
                            FOR UPDATE
                            """
                        )
                        if not matches:
                            return await ctx.followup.send(
                                "There are no open matches (PENDING, ACCEPTED, or ACTIVE) to clear.",
                                ephemeral=True,
                            )

                        for match in matches:
                            player_refunds, bet_refunds = await cancel_open_match_with_refunds(conn, match)
                            total_player_refunds += player_refunds
                            total_bet_refunds += bet_refunds
                            cancelled_matches.append(match)
            except InsufficientFundsError as e:
                await log(f"❌ ERROR — /cancelactives refund failed: {e}")
                return await ctx.followup.send(
                    "Could not clear open matches because escrow was inconsistent. No matches were changed.",
                    ephemeral=True,
                )

            embed = await build_cancelled_match_embed(
                f"A moderator cleared all open matches. Player wagers and spectator bets have been refunded."
            )
            for match in cancelled_matches:
                cancel_challenge_expiry_task(match["match_id"])
                await update_match_message(match["match_id"], embed, view=None)

            match_ids = ", ".join(f"`{m['match_id']}`" for m in cancelled_matches[:20])
            if len(cancelled_matches) > 20:
                match_ids += f", … (+{len(cancelled_matches) - 20} more)"

            await ctx.followup.send(
                f"Cleared **{len(cancelled_matches)}** open match(es). "
                f"Refunded **{total_player_refunds}** player wager(s) and **{total_bet_refunds}** bet(s).\n"
                f"Matches: {match_ids}",
                ephemeral=True,
            )
            await log(
                f"🧹 OPEN MATCHES CLEARED — Mod: {fmt_user(ctx.author)} | Matches: {len(cancelled_matches)} | "
                f"Player refunds: {total_player_refunds} | Bet refunds: {total_bet_refunds} | "
                f"IDs: {', '.join(m['match_id'] for m in cancelled_matches)}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /cancelactives | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")

    @bot.slash_command(description="[MOD] Create a team match between two roles for spectator betting")
    @option("team_one", discord.Role, description="First team role")
    @option("team_two", discord.Role, description="Second team role")
    async def match(ctx: discord.ApplicationContext, team_one: discord.Role, team_two: discord.Role):
        try:
            if not await enforce_channel(ctx):
                return
            if not has_mod_role(ctx):
                return await ctx.respond("You don't have permission to use this command.", ephemeral=True)
            if team_one.id == team_two.id:
                return await ctx.respond("Choose two different team roles.", ephemeral=True)
            if team_one.is_default() or team_two.is_default():
                return await ctx.respond("You cannot use @everyone as a team role.", ephemeral=True)

            await ctx.defer()

            match_id = gen_id()
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    while await conn.fetchrow("SELECT 1 FROM team_matches WHERE match_id = $1", match_id):
                        match_id = gen_id()
                    await conn.execute(
                        """
                        INSERT INTO team_matches (
                            match_id, role_one_id, role_two_id, status, created_by_id, channel_id, created_at
                        )
                        VALUES ($1, $2, $3, 'OPEN', $4, $5, $6)
                        """,
                        match_id,
                        str(team_one.id),
                        str(team_two.id),
                        str(ctx.author.id),
                        str(ctx.channel_id),
                        now_utc(),
                    )

            view = TeamMatchOpenView(match_id, team_one.id, team_two.id)
            get_bot().add_view(view)
            embed = await build_team_match_embed(
                match_id,
                team_one.id,
                team_two.id,
                "OPEN",
                created_by_text=f"Created by {fmt_user(ctx.author)} | Match `{match_id}`",
            )
            # Show role names on buttons when possible
            for child in view.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == f"team_bet:{match_id}:{team_one.id}":
                    child.label = f"Bet on {team_one.name}"[:80]
                elif isinstance(child, discord.ui.Button) and child.custom_id == f"team_bet:{match_id}:{team_two.id}":
                    child.label = f"Bet on {team_two.name}"[:80]

            msg = await ctx.followup.send(embed=embed, view=view, wait=True)
            if msg:
                async with get_db_pool().acquire() as conn:
                    await conn.execute(
                        "UPDATE team_matches SET message_id = $1 WHERE match_id = $2",
                        str(msg.id),
                        match_id,
                    )

            await log(
                f"🏟️ TEAM MATCH CREATED — Mod: {fmt_user(ctx.author)} | {team_one.name} vs {team_two.name} | Match ID: {match_id}"
            )

        except Exception:
            if ctx.response.is_done():
                await ctx.followup.send("Something went wrong.", ephemeral=True)
            else:
                await ctx.respond("Something went wrong.", ephemeral=True)
            await log(f"❌ ERROR — Command: /match | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")
