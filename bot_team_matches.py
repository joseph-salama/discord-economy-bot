import asyncio
from datetime import timedelta

import asyncpg
import discord

from bot_helpers import (
    MIN_TEAM_BET,
    TEAM_MATCH_ACTIVE_TIMEOUT_SECONDS,
    TEAM_MATCH_OPEN_TIMEOUT_SECONDS,
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
    resolve_role_label,
    run_team_match_payout,
    spendable,
    update_team_match_message,
)

_team_open_expiry_tasks: dict[str, asyncio.Task] = {}
_team_active_expiry_tasks: dict[str, asyncio.Task] = {}


async def _team_reply(interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


def cancel_team_match_expiry_task(match_id: str):
    task = _team_open_expiry_tasks.pop(match_id, None)
    if task and not task.done():
        task.cancel()


def cancel_team_match_active_expiry_task(match_id: str):
    task = _team_active_expiry_tasks.pop(match_id, None)
    if task and not task.done():
        task.cancel()


def cancel_all_team_match_expiry_tasks(match_id: str):
    cancel_team_match_expiry_task(match_id)
    cancel_team_match_active_expiry_task(match_id)


async def expire_open_team_match(match_id: str) -> bool:
    """Auto-cancel an OPEN team match and refund bets after timeout."""
    cancel_team_match_expiry_task(match_id)
    try:
        async with get_db_pool().acquire() as conn:
            async with conn.transaction():
                match = await lock_team_match(conn, match_id)
                if not match or match["status"] != "OPEN":
                    return False
                role_one_id = int(match["role_one_id"])
                role_two_id = int(match["role_two_id"])
                refunded = await cancel_team_match_with_refunds(conn, match_id)
    except Exception as e:
        await log(f"❌ ERROR — Team match timeout for {match_id}: {e}")
        return False

    embed = await build_team_match_embed(
        match_id,
        role_one_id,
        role_two_id,
        "CANCELLED",
        created_by_text="Cancelled automatically — betting window expired",
    )
    await update_team_match_message(match_id, embed, view=None)
    await log(
        f"⏰ TEAM MATCH TIMEOUT — Match ID: {match_id} | Bets refunded: {refunded}"
    )
    return True


async def expire_active_team_match(match_id: str) -> bool:
    """Auto-cancel an ACTIVE team match and refund bets after timeout."""
    cancel_all_team_match_expiry_tasks(match_id)
    try:
        async with get_db_pool().acquire() as conn:
            async with conn.transaction():
                match = await lock_team_match(conn, match_id)
                if not match or match["status"] != "ACTIVE":
                    return False
                role_one_id = int(match["role_one_id"])
                role_two_id = int(match["role_two_id"])
                refunded = await cancel_team_match_with_refunds(conn, match_id)
    except Exception as e:
        await log(f"❌ ERROR — Active team match timeout for {match_id}: {e}")
        return False

    embed = await build_team_match_embed(
        match_id,
        role_one_id,
        role_two_id,
        "CANCELLED",
        created_by_text="Cancelled automatically — active match idle too long",
    )
    await update_team_match_message(match_id, embed, view=None)
    await log(
        f"⏰ TEAM MATCH ACTIVE TIMEOUT — Match ID: {match_id} | Bets refunded: {refunded}"
    )
    return True


def schedule_team_match_expiry(match_id: str, delay_seconds: float | None = None):
    cancel_team_match_expiry_task(match_id)
    delay = TEAM_MATCH_OPEN_TIMEOUT_SECONDS if delay_seconds is None else max(0.0, delay_seconds)

    async def _runner():
        try:
            await asyncio.sleep(delay)
            await expire_open_team_match(match_id)
        except asyncio.CancelledError:
            return
        finally:
            _team_open_expiry_tasks.pop(match_id, None)

    _team_open_expiry_tasks[match_id] = asyncio.create_task(_runner())


def schedule_team_match_active_expiry(match_id: str, delay_seconds: float | None = None):
    cancel_team_match_active_expiry_task(match_id)
    delay = TEAM_MATCH_ACTIVE_TIMEOUT_SECONDS if delay_seconds is None else max(0.0, delay_seconds)

    async def _runner():
        try:
            await asyncio.sleep(delay)
            await expire_active_team_match(match_id)
        except asyncio.CancelledError:
            return
        finally:
            _team_active_expiry_tasks.pop(match_id, None)

    _team_active_expiry_tasks[match_id] = asyncio.create_task(_runner())


async def expire_stale_team_matches():
    cutoff = now_utc() - timedelta(seconds=TEAM_MATCH_OPEN_TIMEOUT_SECONDS)
    async with get_db_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT match_id
            FROM team_matches
            WHERE status = 'OPEN' AND created_at <= $1
            """,
            cutoff,
        )
    for row in rows:
        await expire_open_team_match(row["match_id"])


async def expire_stale_active_team_matches():
    cutoff = now_utc() - timedelta(seconds=TEAM_MATCH_ACTIVE_TIMEOUT_SECONDS)
    async with get_db_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT match_id
            FROM team_matches
            WHERE status = 'ACTIVE'
              AND COALESCE(started_at, created_at) <= $1
            """,
            cutoff,
        )
    for row in rows:
        await expire_active_team_match(row["match_id"])


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

        await interaction.response.defer(ephemeral=True)
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match:
                        return await _team_reply(interaction, "This team match no longer exists.")
                    if match["status"] != "OPEN":
                        return await _team_reply(
                            interaction,
                            f"Bets are only open while the match is OPEN (current: {match['status']}).",
                        )
                    if str(self.role_id) not in (match["role_one_id"], match["role_two_id"]):
                        return await _team_reply(interaction, "That team is not part of this match.")

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
                        return await _team_reply(
                            interaction,
                            "You already have a bet on this match. Use **Cancel My Bet** first to change it.",
                        )

                    bettor_row = await lock_user(conn, interaction.user.id)
                    if not bettor_row or spendable(bettor_row) < amount:
                        available = spendable(bettor_row) if bettor_row else 0
                        return await _team_reply(
                            interaction,
                            f"Insufficient funds. You have {fmt(available)} available.",
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
        except asyncpg.UniqueViolationError:
            return await _team_reply(
                interaction,
                "You already have a bet on this match. Use **Cancel My Bet** first to change it.",
            )
        except InsufficientFundsError:
            return await _team_reply(interaction, f"Insufficient funds to bet {fmt(amount)}.")

        embed = await build_team_match_embed(
            self.match_id,
            int(match["role_one_id"]),
            int(match["role_two_id"]),
            "OPEN",
            created_by_text=f"Created by mod | Match `{self.match_id}`",
        )
        open_view = TeamMatchOpenView(
            self.match_id,
            int(match["role_one_id"]),
            int(match["role_two_id"]),
            resolve_role_label(int(match["role_one_id"]), "Team 1"),
            resolve_role_label(int(match["role_two_id"]), "Team 2"),
        )
        get_bot().add_view(open_view)
        await update_team_match_message(self.match_id, embed, view=open_view)
        await interaction.followup.send(
            f"Bet placed: {fmt(amount)} on {await get_role_mention(self.role_id)} for match `{self.match_id}`.",
            ephemeral=True,
        )
        await log(
            f"🎲 TEAM BET — {fmt_user(interaction.user)} bet {fmt(amount)} on role {self.role_id} | Match: {self.match_id} | Bet ID: {bet_id}"
        )


class TeamMatchOpenView(discord.ui.View):
    def __init__(
        self,
        match_id: str,
        role_one_id: int,
        role_two_id: int,
        role_one_label: str | None = None,
        role_two_label: str | None = None,
    ):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.role_one_id = role_one_id
        self.role_two_id = role_two_id
        self.role_one_label = role_one_label or resolve_role_label(role_one_id, "Team 1")
        self.role_two_label = role_two_label or resolve_role_label(role_two_id, "Team 2")

        bet_one = discord.ui.Button(
            label=f"Bet on {self.role_one_label}"[:80],
            style=discord.ButtonStyle.primary,
            emoji="💰",
            custom_id=f"team_bet:{match_id}:{role_one_id}",
            row=0,
        )
        bet_one.callback = self.bet_team_one
        self.add_item(bet_one)

        bet_two = discord.ui.Button(
            label=f"Bet on {self.role_two_label}"[:80],
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
        label = role.name if role else self.role_one_label
        await interaction.response.send_modal(TeamBetModal(self.match_id, self.role_one_id, label))

    async def bet_team_two(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_two_id) if interaction.guild else None
        label = role.name if role else self.role_two_label
        await interaction.response.send_modal(TeamBetModal(self.match_id, self.role_two_id, label))

    async def cancel_my_bet(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match or match["status"] != "OPEN":
                        return await _team_reply(
                            interaction,
                            "Bets can only be cancelled while the match is OPEN.",
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
                        return await _team_reply(interaction, "You don't have an open bet on this match.")
                    deleted = await conn.fetchrow(
                        """
                        DELETE FROM team_bets
                        WHERE bet_id = $1 AND status = 'PENDING'
                        RETURNING *
                        """,
                        bet["bet_id"],
                    )
                    if not deleted:
                        return await _team_reply(interaction, "You don't have an open bet on this match.")
                    await release_escrow(
                        conn,
                        interaction.user.id,
                        bet["amount"],
                        f"team bet cancelled (match {self.match_id})",
                        fmt_user(interaction.user),
                    )
        except InsufficientFundsError:
            return await _team_reply(
                interaction,
                "Could not refund your bet cleanly. Please ask a moderator for help.",
            )

        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "OPEN",
            created_by_text=f"Created by mod | Match `{self.match_id}`",
        )
        await update_team_match_message(self.match_id, embed, view=self)
        await interaction.followup.send(
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

        await interaction.response.defer()
        async with get_db_pool().acquire() as conn:
            async with conn.transaction():
                match = await lock_team_match(conn, self.match_id)
                if not match:
                    return await _team_reply(interaction, "This team match no longer exists.")
                if match["status"] != "OPEN":
                    return await _team_reply(
                        interaction,
                        f"This match cannot be started (current: {match['status']}).",
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
                    return await _team_reply(interaction, "This match could not be started.")

        cancel_team_match_expiry_task(self.match_id)
        schedule_team_match_active_expiry(self.match_id)
        active_view = TeamMatchActiveView(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            self.role_one_label,
            self.role_two_label,
        )
        get_bot().add_view(active_view)
        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "ACTIVE",
            created_by_text=f"Started by {fmt_user(interaction.user)} | Match `{self.match_id}`",
        )
        try:
            await update_team_match_message(self.match_id, embed, view=active_view)
            await interaction.followup.send(
                f"Team match `{self.match_id}` started. Bets are locked.",
                ephemeral=True,
            )
        except Exception as e:
            await log(f"⚠️ TEAM UI EDIT FALLBACK — Match ID: {self.match_id} | Error: {e}")
            await update_team_match_message(self.match_id, embed, view=active_view)
        await log(f"🥊 TEAM MATCH STARTED — Match ID: {self.match_id} | By: {fmt_user(interaction.user)}")

    async def cancel_match(self, interaction: discord.Interaction):
        if not member_has_mod_role(interaction.user):
            return await interaction.response.send_message(
                "Only moderators can cancel team matches.",
                ephemeral=True,
            )

        await interaction.response.defer()
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match or match["status"] not in ("OPEN", "ACTIVE"):
                        return await _team_reply(interaction, "This match cannot be cancelled right now.")
                    refunded = await cancel_team_match_with_refunds(conn, self.match_id)
        except InsufficientFundsError as e:
            await log(f"❌ ERROR — team match cancel failed for {self.match_id}: {e}")
            return await _team_reply(
                interaction,
                "Could not cancel cleanly because escrow was inconsistent.",
            )

        cancel_all_team_match_expiry_tasks(self.match_id)
        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "CANCELLED",
            created_by_text=f"Cancelled by {fmt_user(interaction.user)}",
        )
        await update_team_match_message(self.match_id, embed, view=None)
        await interaction.followup.send(
            f"Team match `{self.match_id}` cancelled. Refunded **{refunded}** bet(s).",
            ephemeral=True,
        )
        await log(
            f"🚫 TEAM MATCH CANCELLED — Match ID: {self.match_id} | By: {fmt_user(interaction.user)} | Bets refunded: {refunded}"
        )


class TeamMatchActiveView(discord.ui.View):
    def __init__(
        self,
        match_id: str,
        role_one_id: int,
        role_two_id: int,
        role_one_label: str | None = None,
        role_two_label: str | None = None,
    ):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.role_one_id = role_one_id
        self.role_two_id = role_two_id
        self.role_one_label = role_one_label or resolve_role_label(role_one_id, "Team 1")
        self.role_two_label = role_two_label or resolve_role_label(role_two_id, "Team 2")

        win_one = discord.ui.Button(
            label=f"{self.role_one_label} Won"[:80],
            style=discord.ButtonStyle.success,
            emoji="🏆",
            custom_id=f"team_win:{match_id}:{role_one_id}",
        )
        win_one.callback = self.team_one_won
        self.add_item(win_one)

        win_two = discord.ui.Button(
            label=f"{self.role_two_label} Won"[:80],
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

        cancel_all_team_match_expiry_tasks(self.match_id)
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

        await interaction.response.defer()
        try:
            async with get_db_pool().acquire() as conn:
                async with conn.transaction():
                    match = await lock_team_match(conn, self.match_id)
                    if not match or match["status"] != "ACTIVE":
                        return await _team_reply(interaction, "This match cannot be cancelled right now.")
                    refunded = await cancel_team_match_with_refunds(conn, self.match_id)
        except InsufficientFundsError as e:
            await log(f"❌ ERROR — team match cancel failed for {self.match_id}: {e}")
            return await _team_reply(
                interaction,
                "Could not cancel cleanly because escrow was inconsistent.",
            )

        cancel_all_team_match_expiry_tasks(self.match_id)
        embed = await build_team_match_embed(
            self.match_id,
            self.role_one_id,
            self.role_two_id,
            "CANCELLED",
            created_by_text=f"Cancelled by {fmt_user(interaction.user)}",
        )
        await update_team_match_message(self.match_id, embed, view=None)
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

    now = now_utc()
    for match in open_matches:
        match_id = match["match_id"]
        role_one_id = int(match["role_one_id"])
        role_two_id = int(match["role_two_id"])
        label_one = resolve_role_label(role_one_id, "Team 1")
        label_two = resolve_role_label(role_two_id, "Team 2")
        bot.add_view(TeamMatchOpenView(match_id, role_one_id, role_two_id, label_one, label_two))
        elapsed = (now - match["created_at"]).total_seconds()
        remaining = TEAM_MATCH_OPEN_TIMEOUT_SECONDS - elapsed
        if remaining <= 0:
            await expire_open_team_match(match_id)
        else:
            schedule_team_match_expiry(match_id, remaining)

    for match in active_matches:
        match_id = match["match_id"]
        role_one_id = int(match["role_one_id"])
        role_two_id = int(match["role_two_id"])
        bot.add_view(
            TeamMatchActiveView(
                match_id,
                role_one_id,
                role_two_id,
                resolve_role_label(role_one_id, "Team 1"),
                resolve_role_label(role_two_id, "Team 2"),
            )
        )
        anchor = match["started_at"] or match["created_at"]
        remaining = TEAM_MATCH_ACTIVE_TIMEOUT_SECONDS - (now - anchor).total_seconds()
        if remaining <= 0:
            await expire_active_team_match(match_id)
        else:
            schedule_team_match_active_expiry(match_id, remaining)

    if open_matches or active_matches:
        await log(
            f"🔁 RESTORED TEAM VIEWS — Open: {len(open_matches)} | Active: {len(active_matches)}"
        )
