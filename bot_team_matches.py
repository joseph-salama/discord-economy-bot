import discord

from bot_helpers import (
    MIN_TEAM_BET,
    InsufficientFundsError,
    build_team_match_embed,
    cancel_team_match_with_refunds,
    debit_escrow,
    ensure_user,
    fmt,
    fmt_user,
    gen_id,
    get_bot,
    get_db_pool,
    get_role_mention,
    lock_team_match,
    lock_user,
    log,
    member_has_mod_role,
    now_utc,
    release_escrow,
    run_team_match_payout,
    spendable,
    update_team_match_message,
)


class TeamBetModal(discord.ui.Modal):
    def __init__(self, match_id: str, role_id: int, role_label: str):
        super().__init__(title=f"Bet on {role_label}"[:45])
        self.match_id = match_id
        self.role_id = role_id
        self.amount_input = discord.ui.InputText(
            label="Bet amount",
            placeholder=f"Minimum {MIN_TEAM_BET}",
            required=True,
            max_length=10,
        )
        self.add_item(self.amount_input)

    async def callback(self, interaction: discord.Interaction):
        raw = (self.amount_input.value or "").strip().replace(",", "")
        try:
            amount = int(raw)
        except ValueError:
            return await interaction.response.send_message(
                "Enter a whole number amount.",
                ephemeral=True,
            )
        if amount < MIN_TEAM_BET:
            return await interaction.response.send_message(
                f"Minimum team bet is {fmt(MIN_TEAM_BET)}.",
                ephemeral=True,
            )

        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match:
                        return await interaction.response.send_message(
                            "This team match no longer exists.",
                            ephemeral=True,
                        )
                    if match["status"] != "OPEN":
                        return await interaction.response.send_message(
                            f"Bets are only open while the match is OPEN (current: {match['status']}).",
                            ephemeral=True,
                        )
                    if str(self.role_id) not in (match["role_one_id"], match["role_two_id"]):
                        return await interaction.response.send_message(
                            "That team is not part of this match.",
                            ephemeral=True,
                        )

                    await ensure_user(conn, interaction.user.id)
                    existing = await conn.fetchrow(
                        """
                        SELECT 1 FROM team_bets
                        WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'
                        """,
                        self.match_id,
                        str(interaction.user.id),
                    )
                    if existing:
                        return await interaction.response.send_message(
                            "You already have a bet on this match. Use **Cancel My Bet** first to change it.",
                            ephemeral=True,
                        )

                    bettor_row = await lock_user(conn, interaction.user.id)
                    if not bettor_row or spendable(bettor_row) < amount:
                        available = spendable(bettor_row) if bettor_row else 0
                        return await interaction.response.send_message(
                            f"Insufficient funds. You have {fmt(available)} available.",
                            ephemeral=True,
                        )

                    bet_id = gen_id(5)
                    while await conn.fetchrow("SELECT 1 FROM team_bets WHERE bet_id = $1", bet_id):
                        bet_id = gen_id(5)

                    await debit_escrow(
                        conn,
                        interaction.user.id,
                        amount,
                        f"team bet escrowed (match {self.match_id})",
                        fmt_user(interaction.user),
                    )
                    await conn.execute(
                        """
                        INSERT INTO team_bets (bet_id, match_id, bettor_id, predicted_role_id, amount, status)
                        VALUES ($1, $2, $3, $4, $5, 'PENDING')
                        """,
                        bet_id,
                        self.match_id,
                        str(interaction.user.id),
                        str(self.role_id),
                        amount,
                    )
        except InsufficientFundsError:
            return await interaction.response.send_message(
                f"Insufficient funds to bet {fmt(amount)}.",
                ephemeral=True,
            )

        embed = await build_team_match_embed(
            self.match_id,
            int(match["role_one_id"]),
            int(match["role_two_id"]),
            "OPEN",
            created_by_text=f"Created by mod | Match `{self.match_id}`",
        )
        await update_team_match_message(
            self.match_id,
            embed,
            view=TeamMatchOpenView(
                self.match_id,
                int(match["role_one_id"]),
                int(match["role_two_id"]),
            ),
        )
        await interaction.response.send_message(
            f"Bet placed: {fmt(amount)} on {await get_role_mention(self.role_id)} for match `{self.match_id}`.",
            ephemeral=True,
        )
        await log(
            f"🎲 TEAM BET — {fmt_user(interaction.user)} bet {fmt(amount)} on role {self.role_id} | Match: {self.match_id} | Bet ID: {bet_id}"
        )


class TeamMatchOpenView(discord.ui.View):
    def __init__(self, match_id: str, role_one_id: int, role_two_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.role_one_id = role_one_id
        self.role_two_id = role_two_id

        bet_one = discord.ui.Button(
            label="Bet on Team 1",
            style=discord.ButtonStyle.primary,
            emoji="💰",
            custom_id=f"team_bet:{match_id}:{role_one_id}",
            row=0,
        )
        bet_one.callback = self.bet_team_one
        self.add_item(bet_one)

        bet_two = discord.ui.Button(
            label="Bet on Team 2",
            style=discord.ButtonStyle.primary,
            emoji="💰",
            custom_id=f"team_bet:{match_id}:{role_two_id}",
            row=0,
        )
        bet_two.callback = self.bet_team_two
        self.add_item(bet_two)

        cancel_bet = discord.ui.Button(
            label="Cancel My Bet",
            style=discord.ButtonStyle.secondary,
            custom_id=f"team_cancel_bet:{match_id}",
            row=1,
        )
        cancel_bet.callback = self.cancel_my_bet
        self.add_item(cancel_bet)

        start_btn = discord.ui.Button(
            label="Start Match",
            style=discord.ButtonStyle.success,
            emoji="▶️",
            custom_id=f"team_start:{match_id}",
            row=1,
        )
        start_btn.callback = self.start_match
        self.add_item(start_btn)

        cancel_match = discord.ui.Button(
            label="Cancel Match",
            style=discord.ButtonStyle.danger,
            custom_id=f"team_cancel_match:{match_id}",
            row=1,
        )
        cancel_match.callback = self.cancel_match
        self.add_item(cancel_match)

    async def bet_team_one(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_one_id) if interaction.guild else None
        label = role.name if role else "Team 1"
        await interaction.response.send_modal(TeamBetModal(self.match_id, self.role_one_id, label))

    async def bet_team_two(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_two_id) if interaction.guild else None
        label = role.name if role else "Team 2"
        await interaction.response.send_modal(TeamBetModal(self.match_id, self.role_two_id, label))

    async def cancel_my_bet(self, interaction: discord.Interaction):
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match or match["status"] != "OPEN":
                        return await interaction.response.send_message(
                            "Bets can only be cancelled while the match is OPEN.",
                            ephemeral=True,
                        )
                    bet = await conn.fetchrow(
                        """
                        SELECT * FROM team_bets
                        WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'
                        FOR UPDATE
                        """,
                        self.match_id,
                        str(interaction.user.id),
                    )
                    if not bet:
                        return await interaction.response.send_message(
                            "You don't have an open bet on this match.",
                            ephemeral=True,
                        )
                    deleted = await conn.fetchrow(
                        """
                        DELETE FROM team_bets
                        WHERE bet_id = $1 AND status = 'PENDING'
                        RETURNING *
                        """,
                        bet["bet_id"],
                    )
                    if not deleted:
                        return await interaction.response.send_message(
                            "You don't have an open bet on this match.",
                            ephemeral=True,
                        )
                    await release_escrow(
                        conn,
                        interaction.user.id,
                        bet["amount"],
                        f"team bet cancelled (match {self.match_id})",
                        fmt_user(interaction.user),
                    )
        except InsufficientFundsError:
            return await interaction.response.send_message(
                "Could not refund your bet cleanly. Please ask a moderator for help.",
                ephemeral=True,
            )

        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "OPEN",
            created_by_text=f"Created by mod | Match `{self.match_id}`",
        )
        await update_team_match_message(self.match_id, embed, view=self)
        await interaction.response.send_message(
            f"Your bet of {fmt(bet['amount'])} was cancelled and refunded.",
            ephemeral=True,
        )
        await log(
            f"🚫 TEAM BET CANCELLED — {fmt_user(interaction.user)} refunded {fmt(bet['amount'])} on match {self.match_id}"
        )

    async def start_match(self, interaction: discord.Interaction):
        if not member_has_mod_role(interaction.user):
            return await interaction.response.send_message(
                "Only moderators can start team matches.",
                ephemeral=True,
            )

        async with get_db_pool().acquire() as conn:
            async with conn.transaction():
                match = await lock_team_match(conn, self.match_id)
                if not match:
                    return await interaction.response.send_message(
                        "This team match no longer exists.",
                        ephemeral=True,
                    )
                if match["status"] != "OPEN":
                    return await interaction.response.send_message(
                        f"This match cannot be started (current: {match['status']}).",
                        ephemeral=True,
                    )
                started = await conn.fetchrow(
                    """
                    UPDATE team_matches
                    SET status = 'ACTIVE', started_at = $1
                    WHERE match_id = $2 AND status = 'OPEN'
                    RETURNING *
                    """,
                    now_utc(),
                    self.match_id,
                )
                if not started:
                    return await interaction.response.send_message(
                        "This match could not be started.",
                        ephemeral=True,
                    )

        active_view = TeamMatchActiveView(self.match_id, self.role_one_id, self.role_two_id)
        get_bot().add_view(active_view)
        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "ACTIVE",
            created_by_text=f"Started by {fmt_user(interaction.user)} | Match `{self.match_id}`",
        )
        await interaction.response.edit_message(embed=embed, view=active_view)
        await log(f"🥊 TEAM MATCH STARTED — Match ID: {self.match_id} | By: {fmt_user(interaction.user)}")

    async def cancel_match(self, interaction: discord.Interaction):
        if not member_has_mod_role(interaction.user):
            return await interaction.response.send_message(
                "Only moderators can cancel team matches.",
                ephemeral=True,
            )

        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match or match["status"] not in ("OPEN", "ACTIVE"):
                        return await interaction.response.send_message(
                            "This match cannot be cancelled right now.",
                            ephemeral=True,
                        )
                    refunded = await cancel_team_match_with_refunds(conn, self.match_id)
        except InsufficientFundsError as e:
            await log(f"❌ ERROR — team match cancel failed for {self.match_id}: {e}")
            return await interaction.response.send_message(
                "Could not cancel cleanly because escrow was inconsistent.",
                ephemeral=True,
            )

        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "CANCELLED",
            created_by_text=f"Cancelled by {fmt_user(interaction.user)}",
        )
        await interaction.response.edit_message(embed=embed, view=None)
        await interaction.followup.send(
            f"Team match `{self.match_id}` cancelled. Refunded **{refunded}** bet(s).",
            ephemeral=True,
        )
        await log(
            f"🚫 TEAM MATCH CANCELLED — Match ID: {self.match_id} | By: {fmt_user(interaction.user)} | Bets refunded: {refunded}"
        )


class TeamMatchActiveView(discord.ui.View):
    def __init__(self, match_id: str, role_one_id: int, role_two_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.role_one_id = role_one_id
        self.role_two_id = role_two_id

        guild = None
        try:
            # Labels prefer cached role names when available
            for g in get_bot().guilds:
                if g.get_role(role_one_id) or g.get_role(role_two_id):
                    guild = g
                    break
        except Exception:
            guild = None

        role_one = guild.get_role(role_one_id) if guild else None
        role_two = guild.get_role(role_two_id) if guild else None
        label_one = f"{role_one.name} Won" if role_one else "Team 1 Won"
        label_two = f"{role_two.name} Won" if role_two else "Team 2 Won"

        win_one = discord.ui.Button(
            label=label_one[:80],
            style=discord.ButtonStyle.success,
            emoji="🏆",
            custom_id=f"team_win:{match_id}:{role_one_id}",
        )
        win_one.callback = self.team_one_won
        self.add_item(win_one)

        win_two = discord.ui.Button(
            label=label_two[:80],
            style=discord.ButtonStyle.success,
            emoji="🏆",
            custom_id=f"team_win:{match_id}:{role_two_id}",
        )
        win_two.callback = self.team_two_won
        self.add_item(win_two)

        cancel_match = discord.ui.Button(
            label="Cancel Match",
            style=discord.ButtonStyle.danger,
            custom_id=f"team_cancel_active:{match_id}",
        )
        cancel_match.callback = self.cancel_match
        self.add_item(cancel_match)

    async def complete(self, interaction: discord.Interaction, winner_role_id: int):
        if not member_has_mod_role(interaction.user):
            return await interaction.response.send_message(
                "Only moderators can declare the winner.",
                ephemeral=True,
            )

        await interaction.response.defer()
        payout_embed = None
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match:
                        return await interaction.followup.send(
                            "This team match no longer exists.",
                            ephemeral=True,
                        )
                    if match["status"] != "ACTIVE":
                        return await interaction.followup.send(
                            f"This match is not active (current: {match['status']}).",
                            ephemeral=True,
                        )
                    if str(winner_role_id) not in (match["role_one_id"], match["role_two_id"]):
                        return await interaction.followup.send(
                            "Winner must be one of the two teams.",
                            ephemeral=True,
                        )

                    completed = await conn.fetchrow(
                        """
                        UPDATE team_matches
                        SET status = 'COMPLETED', winner_role_id = $1
                        WHERE match_id = $2 AND status = 'ACTIVE'
                        RETURNING *
                        """,
                        str(winner_role_id),
                        self.match_id,
                    )
                    if not completed:
                        return await interaction.followup.send(
                            "This match was already completed.",
                            ephemeral=True,
                        )
                    payout_embed = await run_team_match_payout(
                        conn,
                        self.match_id,
                        str(winner_role_id),
                        mod_tag=fmt_user(interaction.user),
                    )
        except InsufficientFundsError as e:
            await log(f"❌ ERROR — team match payout failed for {self.match_id}: {e}")
            return await interaction.followup.send(
                "Payout failed because escrowed funds were inconsistent.",
                ephemeral=True,
            )

        if payout_embed and interaction.channel:
            await interaction.channel.send(embed=payout_embed)

        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "COMPLETED",
            winner_role_id=winner_role_id,
            created_by_text=f"Resolved by {fmt_user(interaction.user)}",
        )
        await update_team_match_message(self.match_id, embed, view=None)
        await interaction.followup.send(
            f"Team match `{self.match_id}` completed. Winner: {await get_role_mention(winner_role_id)}",
            ephemeral=True,
        )

    async def team_one_won(self, interaction: discord.Interaction):
        await self.complete(interaction, self.role_one_id)

    async def team_two_won(self, interaction: discord.Interaction):
        await self.complete(interaction, self.role_two_id)

    async def cancel_match(self, interaction: discord.Interaction):
        if not member_has_mod_role(interaction.user):
            return await interaction.response.send_message(
                "Only moderators can cancel team matches.",
                ephemeral=True,
            )

        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match or match["status"] != "ACTIVE":
                        return await interaction.response.send_message(
                            "This match cannot be cancelled right now.",
                            ephemeral=True,
                        )
                    refunded = await cancel_team_match_with_refunds(conn, self.match_id)
        except InsufficientFundsError as e:
            await log(f"❌ ERROR — team match cancel failed for {self.match_id}: {e}")
            return await interaction.response.send_message(
                "Could not cancel cleanly because escrow was inconsistent.",
                ephemeral=True,
            )

        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "CANCELLED",
            created_by_text=f"Cancelled by {fmt_user(interaction.user)}",
        )
        await interaction.response.edit_message(embed=embed, view=None)
        await interaction.followup.send(
            f"Team match `{self.match_id}` cancelled. Refunded **{refunded}** bet(s).",
            ephemeral=True,
        )
        await log(
            f"🚫 TEAM MATCH CANCELLED — Match ID: {self.match_id} | By: {fmt_user(interaction.user)} | Bets refunded: {refunded}"
        )


async def restore_team_match_views():
    bot = get_bot()
    async with get_db_pool().acquire() as conn:
        open_matches = await conn.fetch("SELECT * FROM team_matches WHERE status = 'OPEN'")
        active_matches = await conn.fetch("SELECT * FROM team_matches WHERE status = 'ACTIVE'")

    for match in open_matches:
        bot.add_view(
            TeamMatchOpenView(
                match["match_id"],
                int(match["role_one_id"]),
                int(match["role_two_id"]),
            )
        )
    for match in active_matches:
        bot.add_view(
            TeamMatchActiveView(
                match["match_id"],
                int(match["role_one_id"]),
                int(match["role_two_id"]),
            )
        )

    if open_matches or active_matches:
        await log(
            f"🔁 RESTORED TEAM VIEWS — Open: {len(open_matches)} | Active: {len(active_matches)}"
        )
