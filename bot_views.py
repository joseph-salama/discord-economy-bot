import discord

from bot_helpers import (
    CHALLENGE_TIMEOUT_SECONDS,
    CURRENCY_NAME,
    MODERATOR_ROLE_ID,
    build_accepted_match_embed,
    build_top_embed,
    debit_escrow,
    ensure_user,
    fmt,
    fmt_user,
    get_bot,
    get_db_pool,
    get_user,
    has_mod_role,
    log,
    now_utc,
    release_escrow,
    run_payout,
    spendable,
    update_match_message,
)


class ChallengeView(discord.ui.View):
    def __init__(self, match_id: str, challenger_id: int, opponent_id: int):
        super().__init__(timeout=CHALLENGE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id

    async def on_timeout(self):
        async with get_db_pool().acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match or match["status"] != "PENDING":
                return
            await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", self.match_id)
            challenger = await get_bot().fetch_user(self.challenger_id)
            await release_escrow(conn, self.challenger_id, match["wager_amount"], "battle timeout refund", fmt_user(challenger))
            channel = get_bot().get_channel(int(match["channel_id"]))
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
                f"⏰ BATTLE TIMEOUT — Match ID: {self.match_id} | Challenger: {fmt_user(challenger)} | Wager refunded: {fmt(match['wager_amount'])}"
            )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message("This is not your battle to accept.", ephemeral=True)
        async with get_db_pool().acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match or match["status"] != "PENDING":
                return await interaction.response.send_message("This challenge is no longer valid.", ephemeral=True)

            opponent = interaction.user
            await ensure_user(conn, opponent.id)
            opp_row = await get_user(conn, opponent.id)
            avail = spendable(opp_row)
            if avail < match["wager_amount"]:
                return await interaction.response.send_message(
                    f"You don't have enough {CURRENCY_NAME} to accept. You need {fmt(match['wager_amount'])} but only have {fmt(avail)} available.",
                    ephemeral=True,
                )

            await debit_escrow(conn, opponent.id, match["wager_amount"], f"battle wager escrowed (match {self.match_id})", fmt_user(opponent))
            await conn.execute(
                "UPDATE matches SET status = 'ACCEPTED', accepted_at = $1 WHERE match_id = $2",
                now_utc(),
                self.match_id,
            )

        challenger = await get_bot().fetch_user(self.challenger_id)
        embed = await build_accepted_match_embed(
            self.match_id,
            self.challenger_id,
            self.opponent_id,
            match["wager_amount"],
        )
        self.stop()
        await interaction.response.edit_message(
            embed=embed,
            view=MatchStartView(self.match_id, self.challenger_id, self.opponent_id),
        )
        await log(
            f"✅ BATTLE ACCEPTED — Match ID: {self.match_id} | {fmt_user(challenger)} vs {fmt_user(opponent)} | Wager: {fmt(match['wager_amount'])}"
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message("This is not your battle to decline.", ephemeral=True)
        async with get_db_pool().acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match or match["status"] != "PENDING":
                return await interaction.response.send_message("This challenge is no longer valid.", ephemeral=True)
            challenger = await get_bot().fetch_user(self.challenger_id)
            await release_escrow(conn, self.challenger_id, match["wager_amount"], "battle declined refund", fmt_user(challenger))
            await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", self.match_id)

        embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"{interaction.user.mention} declined the battle. Wager refunded.",
            color=discord.Color.red(),
        )
        self.stop()
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

    @discord.ui.button(label="Start Match", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start_match(self, button: discord.ui.Button, interaction: discord.Interaction):
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
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match:
                return await interaction.response.send_message("This match no longer exists.", ephemeral=True)
            if match["status"] != "ACCEPTED":
                return await interaction.response.send_message(
                    f"This match cannot be started right now (current: {match['status']}).",
                    ephemeral=True,
                )

            await conn.execute(
                "UPDATE matches SET status = 'ACTIVE', started_at = $1 WHERE match_id = $2",
                now_utc(),
                self.match_id,
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
        challenger_label = f"{challenger_user.name}" if challenger_user else "Challenger"
        opponent_label = f"{opponent_user.name}" if opponent_user else "Opponent"

        challenger_button = discord.ui.Button(
            label=f"{challenger_label} Won",
            style=discord.ButtonStyle.success,
            emoji="🏆",
        )
        challenger_button.callback = self.challenger_won
        self.add_item(challenger_button)

        opponent_button = discord.ui.Button(
            label=f"{opponent_label} Won",
            style=discord.ButtonStyle.success,
            emoji="🏆",
        )
        opponent_button.callback = self.opponent_won
        self.add_item(opponent_button)

    async def complete_match(self, interaction: discord.Interaction, winner_id: int):
        if interaction.user.id not in (self.challenger_id, self.opponent_id):
            return await interaction.response.send_message("Only one of the two players can report the winner.", ephemeral=True)

        async with get_db_pool().acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match:
                return await interaction.response.send_message("This match no longer exists.", ephemeral=True)
            if match["status"] != "ACTIVE":
                return await interaction.response.send_message(
                    f"This match is not active right now (current: {match['status']}).",
                    ephemeral=True,
                )

            await conn.execute(
                "UPDATE matches SET status = 'COMPLETED', winner_id = $1, reported_by_id = $2 WHERE match_id = $3",
                str(winner_id),
                str(interaction.user.id),
                self.match_id,
            )

            await interaction.response.defer()
            await run_payout(conn, self.match_id, str(winner_id), interaction.channel)

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

    async def refresh_buttons(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "top_prev":
                    child.disabled = self.page <= 1
                elif child.custom_id == "top_next":
                    child.disabled = self.page >= self.total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who used /top can change pages.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="top_prev")
    async def previous_page(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page -= 1
        embed, self.total_pages = await build_top_embed(self.page)
        self.page = max(1, min(self.page, self.total_pages))
        await self.refresh_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="top_next")
    async def next_page(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.page += 1
        embed, self.total_pages = await build_top_embed(self.page)
        self.page = max(1, min(self.page, self.total_pages))
        await self.refresh_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
