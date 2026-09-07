import asyncio
from datetime import timedelta

import discord

from bot_helpers import (
    CHALLENGE_TIMEOUT_SECONDS,
    CURRENCY_NAME,
    MODERATOR_ROLE_ID,
    InsufficientFundsError,
    build_accepted_match_embed,
    build_top_embed,
    cancel_match_if_pending,
    complete_match_if_active,
    debit_escrow,
    ensure_user,
    fmt,
    fmt_user,
    gen_id,
    get_bot,
    get_db_pool,
    lock_match,
    lock_user,
    log,
    now_utc,
    release_escrow,
    run_payout,
    spendable,
    update_match_message,
)

# match_id -> expiry task, so we can cancel if accepted/declined early
_challenge_expiry_tasks: dict[str, asyncio.Task] = {}


def cancel_challenge_expiry_task(match_id: str):
    task = _challenge_expiry_tasks.pop(match_id, None)
    if task and not task.done():
        task.cancel()


async def expire_pending_challenge(match_id: str) -> bool:
    """Cancel a PENDING challenge and refund the challenger. Safe to call more than once."""
    cancel_challenge_expiry_task(match_id)
    try:
        async with get_db_pool().acquire() as conn:
            async with conn.transaction():
                match = await cancel_match_if_pending(conn, match_id)
                if not match:
                    return False
                challenger = await get_bot().fetch_user(int(match["challenger_id"]))
                await release_escrow(
                    conn,
                    int(match["challenger_id"]),
                    match["wager_amount"],
                    "battle timeout refund",
                    fmt_user(challenger),
                )
    except Exception as e:
        await log(f"❌ ERROR — Challenge timeout for match {match_id}: {e}")
        return False

    channel = get_bot().get_channel(int(match["channel_id"]))
    if channel is None:
        try:
            channel = await get_bot().fetch_channel(int(match["channel_id"]))
        except Exception:
            channel = None
    if channel and match["message_id"]:
        try:
            msg = await channel.fetch_message(int(match["message_id"]))
            embed = discord.Embed(
                title="⚔️ Challenge Expired",
                description=f"The challenge timed out. {challenger.mention}'s wager has been refunded.",
                color=discord.Color.dark_gray(),
            )
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass

    await log(
        f"⏰ BATTLE TIMEOUT — Match ID: {match_id} | Challenger: {fmt_user(challenger)} | Wager refunded: {fmt(match['wager_amount'])}"
    )
    return True


def schedule_challenge_expiry(match_id: str, delay_seconds: float | None = None):
    """Schedule an in-process expiry; survives only while the bot is running."""
    cancel_challenge_expiry_task(match_id)
    delay = CHALLENGE_TIMEOUT_SECONDS if delay_seconds is None else max(0.0, delay_seconds)

    async def _runner():
        try:
            await asyncio.sleep(delay)
            await expire_pending_challenge(match_id)
        except asyncio.CancelledError:
            return
        finally:
            _challenge_expiry_tasks.pop(match_id, None)

    _challenge_expiry_tasks[match_id] = asyncio.create_task(_runner())


async def expire_stale_challenges():
    """Expire any PENDING challenges past their created_at + timeout (covers restarts)."""
    cutoff = now_utc() - timedelta(seconds=CHALLENGE_TIMEOUT_SECONDS)
    async with get_db_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT match_id
            FROM matches
            WHERE status = 'PENDING' AND created_at <= $1
            """,
            cutoff,
        )
    for row in rows:
        await expire_pending_challenge(row["match_id"])


async def restore_persistent_views():
    """Re-register match button views after a restart so Start/Report/Accept still work."""
    bot = get_bot()
    async with get_db_pool().acquire() as conn:
        pending = await conn.fetch("SELECT * FROM matches WHERE status = 'PENDING'")
        accepted = await conn.fetch("SELECT * FROM matches WHERE status = 'ACCEPTED'")
        active = await conn.fetch("SELECT * FROM matches WHERE status = 'ACTIVE'")

    now = now_utc()
    for match in pending:
        match_id = match["match_id"]
        view = ChallengeView(
            match_id,
            int(match["challenger_id"]),
            int(match["opponent_id"]),
        )
        bot.add_view(view)
        elapsed = (now - match["created_at"]).total_seconds()
        remaining = CHALLENGE_TIMEOUT_SECONDS - elapsed
        if remaining <= 0:
            await expire_pending_challenge(match_id)
        else:
            schedule_challenge_expiry(match_id, remaining)

    for match in accepted:
        bot.add_view(
            MatchStartView(
                match["match_id"],
                int(match["challenger_id"]),
                int(match["opponent_id"]),
            )
        )

    for match in active:
        bot.add_view(
            MatchReportView(
                match["match_id"],
                int(match["challenger_id"]),
                int(match["opponent_id"]),
            )
        )

    await log(
        f"🔁 RESTORED VIEWS — Pending: {len(pending)} | Accepted: {len(accepted)} | Active: {len(active)}"
    )


class ChallengeView(discord.ui.View):
    def __init__(self, match_id: str, challenger_id: int, opponent_id: int):
        # timeout=None so Accept/Decline survive bot restarts via bot.add_view()
        super().__init__(timeout=None)
        self.match_id = match_id
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id

        accept_btn = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"match_accept:{match_id}",
        )
        accept_btn.callback = self.accept
        self.add_item(accept_btn)

        decline_btn = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"match_decline:{match_id}",
        )
        decline_btn.callback = self.decline
        self.add_item(decline_btn)

    async def accept(self, interaction: discord.Interaction):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message("This is not your battle to accept.", ephemeral=True)

        opponent = interaction.user
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_match(conn, self.match_id)
                    if not match or match["status"] != "PENDING":
                        return await interaction.response.send_message(
                            "This challenge is no longer valid.",
                            ephemeral=True,
                        )

                    await ensure_user(conn, opponent.id)
                    opp_row = await lock_user(conn, opponent.id)
                    if not opp_row:
                        return await interaction.response.send_message(
                            "Could not load your balance. Please try again.",
                            ephemeral=True,
                        )
                    avail = spendable(opp_row)
                    if avail < match["wager_amount"]:
                        return await interaction.response.send_message(
                            f"You don't have enough {CURRENCY_NAME} to accept. You need {fmt(match['wager_amount'])} but only have {fmt(avail)} available.",
                            ephemeral=True,
                        )

                    accepted = await conn.fetchrow(
                        """
                        UPDATE matches
                        SET status = 'ACCEPTED', accepted_at = $1
                        WHERE match_id = $2 AND status = 'PENDING'
                        RETURNING *
                        """,
                        now_utc(),
                        self.match_id,
                    )
                    if not accepted:
                        return await interaction.response.send_message(
                            "This challenge is no longer valid.",
                            ephemeral=True,
                        )

                    await debit_escrow(
                        conn,
                        opponent.id,
                        match["wager_amount"],
                        f"battle wager escrowed (match {self.match_id})",
                        fmt_user(opponent),
                    )
        except InsufficientFundsError:
            return await interaction.response.send_message(
                f"You don't have enough {CURRENCY_NAME} to accept this challenge.",
                ephemeral=True,
            )

        cancel_challenge_expiry_task(self.match_id)
        challenger = await get_bot().fetch_user(self.challenger_id)
        embed = await build_accepted_match_embed(
            self.match_id,
            self.challenger_id,
            self.opponent_id,
            match["wager_amount"],
        )
        start_view = MatchStartView(self.match_id, self.challenger_id, self.opponent_id)
        get_bot().add_view(start_view)
        await interaction.response.edit_message(embed=embed, view=start_view)
        await log(
            f"✅ BATTLE ACCEPTED — Match ID: {self.match_id} | {fmt_user(challenger)} vs {fmt_user(opponent)} | Wager: {fmt(match['wager_amount'])}"
        )

    async def decline(self, interaction: discord.Interaction):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message("This is not your battle to decline.", ephemeral=True)

        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await cancel_match_if_pending(conn, self.match_id)
                    if not match:
                        return await interaction.response.send_message(
                            "This challenge is no longer valid.",
                            ephemeral=True,
                        )
                    challenger = await get_bot().fetch_user(self.challenger_id)
                    await release_escrow(
                        conn,
                        self.challenger_id,
                        match["wager_amount"],
                        "battle declined refund",
                        fmt_user(challenger),
                    )
        except InsufficientFundsError:
            return await interaction.response.send_message(
                "This challenge could not be declined cleanly. Please ask a moderator for help.",
                ephemeral=True,
            )

        cancel_challenge_expiry_task(self.match_id)
        embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"{interaction.user.mention} declined the battle. Wager refunded.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        await log(
            f"❌ BATTLE DECLINED — Match ID: {self.match_id} | Declined by: {fmt_user(interaction.user)} | Wager refunded: {fmt(match['wager_amount'])}"
        )


class MatchStartView(discord.ui.View):
    def __init__(self, match_id: str, challenger_id: int, opponent_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id

        start_btn = discord.ui.Button(
            label="Start Match",
            style=discord.ButtonStyle.primary,
            emoji="▶️",
            custom_id=f"match_start:{match_id}",
        )
        start_btn.callback = self.start_match
        self.add_item(start_btn)

    async def start_match(self, interaction: discord.Interaction):
        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        )
        is_mod = bool(member and any(r.id == MODERATOR_ROLE_ID for r in member.roles))
        if interaction.user.id not in (self.challenger_id, self.opponent_id) and not is_mod:
            return await interaction.response.send_message(
                "Only one of the two players or a moderator can start this match.",
                ephemeral=True,
            )

        async with get_db_pool().acquire() as conn:
            async with conn.transaction():
                match = await lock_match(conn, self.match_id)
                if not match:
                    return await interaction.response.send_message("This match no longer exists.", ephemeral=True)
                if match["status"] != "ACCEPTED":
                    return await interaction.response.send_message(
                        f"This match cannot be started right now (current: {match['status']}).",
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
                    self.match_id,
                )
                if not started:
                    return await interaction.response.send_message(
                        "This match cannot be started right now.",
                        ephemeral=True,
                    )

        challenger = await get_bot().fetch_user(self.challenger_id)
        opponent = await get_bot().fetch_user(self.opponent_id)
        embed = discord.Embed(
            title="🥊 Match Started!",
            description=f"{challenger.mention} vs {opponent.mention}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Match ID", value=self.match_id, inline=True)
        embed.add_field(name="Status", value="**ACTIVE**", inline=True)
        embed.add_field(
            name="Report Winner",
            value="One of the two players can press the winner button below when the match is over.",
            inline=False,
        )

        view = MatchReportView(self.match_id, self.challenger_id, self.opponent_id)
        get_bot().add_view(view)
        await interaction.response.edit_message(embed=embed, view=view)
        await log(f"🥊 MATCH STARTED — Match ID: {self.match_id} | Started by: {fmt_user(interaction.user)}")


class MatchReportView(discord.ui.View):
    def __init__(self, match_id: str, challenger_id: int, opponent_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id

        challenger_user = get_bot().get_user(challenger_id)
        opponent_user = get_bot().get_user(opponent_id)
        challenger_label = challenger_user.name if challenger_user else "Challenger"
        opponent_label = opponent_user.name if opponent_user else "Opponent"

        challenger_button = discord.ui.Button(
            label=f"{challenger_label} Won",
            style=discord.ButtonStyle.success,
            emoji="🏆",
            custom_id=f"match_report:{match_id}:challenger",
        )
        challenger_button.callback = self.challenger_won
        self.add_item(challenger_button)

        opponent_button = discord.ui.Button(
            label=f"{opponent_label} Won",
            style=discord.ButtonStyle.success,
            emoji="🏆",
            custom_id=f"match_report:{match_id}:opponent",
        )
        opponent_button.callback = self.opponent_won
        self.add_item(opponent_button)

    async def complete_match(self, interaction: discord.Interaction, winner_id: int):
        if interaction.user.id not in (self.challenger_id, self.opponent_id):
            return await interaction.response.send_message("Only one of the two players can report the winner.", ephemeral=True)

        await interaction.response.defer()
        payout_embed = None
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_match(conn, self.match_id)
                    if not match:
                        return await interaction.followup.send("This match no longer exists.", ephemeral=True)
                    if match["status"] != "ACTIVE":
                        return await interaction.followup.send(
                            f"This match is not active right now (current: {match['status']}).",
                            ephemeral=True,
                        )

                    completed = await complete_match_if_active(
                        conn,
                        self.match_id,
                        str(winner_id),
                        str(interaction.user.id),
                    )
                    if not completed:
                        return await interaction.followup.send(
                            "This match was already completed by someone else.",
                            ephemeral=True,
                        )

                    payout_embed = await run_payout(conn, self.match_id, str(winner_id))
        except InsufficientFundsError as e:
            await log(f"❌ ERROR — Match report payout failed for {self.match_id}: {e}")
            return await interaction.followup.send(
                "Payout failed because escrowed funds were inconsistent. Please ask a moderator to use `/resolve`.",
                ephemeral=True,
            )

        if payout_embed and interaction.channel:
            await interaction.channel.send(embed=payout_embed)

        winner_user = await get_bot().fetch_user(winner_id)
        challenger = await get_bot().fetch_user(self.challenger_id)
        opponent = await get_bot().fetch_user(self.opponent_id)
        embed = discord.Embed(
            title="⚔️ Match Complete!",
            description=f"{challenger.mention} vs {opponent.mention}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Match ID", value=self.match_id, inline=True)
        embed.add_field(name="Winner", value=winner_user.mention, inline=True)
        embed.add_field(name="Status", value="**COMPLETED**", inline=True)

        await update_match_message(self.match_id, embed, view=None)
        await interaction.followup.send(f"Match `{self.match_id}` has been completed!", ephemeral=True)

    async def challenger_won(self, interaction: discord.Interaction):
        await self.complete_match(interaction, self.challenger_id)

    async def opponent_won(self, interaction: discord.Interaction):
        await self.complete_match(interaction, self.opponent_id)


class TopLeaderboardView(discord.ui.View):
    def __init__(self, author_id: int, page: int = 1):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.page = page
        self.total_pages = 1
        self.message: discord.Message | None = None

        # Unique custom_ids so concurrent /top views don't collide in the view store
        uid = f"{author_id}:{gen_id(6)}"
        self.prev_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id=f"top_prev:{uid}",
        )
        self.prev_button.callback = self.previous_page
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id=f"top_next:{uid}",
        )
        self.next_button.callback = self.next_page
        self.add_item(self.next_button)

    async def refresh_buttons(self):
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who used /top can change pages.", ephemeral=True)
            return False
        return True

    async def previous_page(self, interaction: discord.Interaction):
        self.page -= 1
        embed, self.total_pages = await build_top_embed(self.page)
        self.page = max(1, min(self.page, self.total_pages))
        await self.refresh_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        embed, self.total_pages = await build_top_embed(self.page)
        self.page = max(1, min(self.page, self.total_pages))
        await self.refresh_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        self.prev_button.disabled = True
        self.next_button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
