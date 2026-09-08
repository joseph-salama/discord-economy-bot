"""Money ledger, escrow, payouts, and match settlement."""
import asyncpg
import discord

from bot_runtime import fmt, fmt_user, get_bot, log, spendable


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

    bets = await conn.fetch("SELECT * FROM bets WHERE match_id = $1", match_id)
    user_ids = [int(previous_winner_id), int(previous_loser_id)]
    user_ids.extend(int(bet["bettor_id"]) for bet in bets)
    locked = await lock_users_ordered(conn, *user_ids)

    winner_row = locked.get(int(previous_winner_id))
    if not winner_row or winner_row["balance"] < wager:
        have = winner_row["balance"] if winner_row else 0
        raise PayoutReverseError(
            f"Cannot reverse match `{match_id}`: {fmt_user(previous_winner)} only has {fmt(have)} "
            f"but needs {fmt(wager)} still available to return winnings."
        )

    for bet in bets:
        if bet["status"] != "WON":
            continue
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        payout = int(bet["payout_amount"] or 0)
        bettor_row = locked.get(int(bet["bettor_id"]))
        if not bettor_row or bettor_row["balance"] < payout:
            have = bettor_row["balance"] if bettor_row else 0
            raise PayoutReverseError(
                f"Cannot reverse match `{match_id}`: {fmt_user(bettor)} only has {fmt(have)} "
                f"but needs {fmt(payout)} still available to return bet winnings."
            )
        # After reclaiming winnings, re-escrowing the stake must still satisfy balance >= escrow.
        if bettor_row["balance"] - payout < bettor_row["escrow"] + bet["amount"]:
            raise PayoutReverseError(
                f"Cannot reverse match `{match_id}`: {fmt_user(bettor)} cannot re-escrow "
                f"{fmt(bet['amount'])} after returning {fmt(payout)} winnings."
            )

    for bet in bets:
        if bet["status"] != "REFUNDED":
            continue
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        bettor_row = locked.get(int(bet["bettor_id"]))
        avail = spendable(bettor_row) if bettor_row else 0
        if not bettor_row or avail < bet["amount"]:
            raise PayoutReverseError(
                f"Cannot reverse match `{match_id}`: {fmt_user(bettor)} only has {fmt(avail)} "
                f"available to re-escrow a previously refunded bet of {fmt(bet['amount'])}."
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
            payout = int(bet["payout_amount"] or 0)
            updated = await conn.fetchrow(
                """
                UPDATE users
                SET balance = balance - $1, escrow = escrow + $2
                WHERE user_id = $3
                  AND balance >= $1
                  AND balance - $1 >= escrow + $2
                RETURNING user_id
                """,
                payout,
                bet["amount"],
                bet["bettor_id"],
            )
            if not updated:
                raise PayoutReverseError(
                    f"Cannot reverse match `{match_id}`: failed to reclaim bet winnings from {fmt_user(bettor)}."
                )
            await log(
                f"↩️ BET PAYOUT REVERSED — {fmt_user(bettor)} returned {fmt(payout)} winnings "
                f"and had {fmt(bet['amount'])} re-escrowed (match {match_id})"
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
        elif bet["status"] == "REFUNDED":
            await debit_escrow(
                conn,
                int(bet["bettor_id"]),
                bet["amount"],
                f"bet re-escrowed after reverse (match {match_id})",
                fmt_user(bettor),
            )
            await log(
                f"↩️ BET REFUND REVERSED — {fmt_user(bettor)} re-escrowed {fmt(bet['amount'])} (match {match_id})"
            )

    await conn.execute(
        """
        UPDATE bets
        SET status = 'PENDING', payout_amount = 0
        WHERE match_id = $1 AND status IN ('WON', 'LOST', 'REFUNDED')
        """,
        match_id,
    )
    await conn.execute(
        "UPDATE matches SET proposed_winner_id = NULL WHERE match_id = $1",
        match_id,
    )


def _pari_mutuel_shares(stakes: list[int], pot: int) -> list[int]:
    """Split `pot` across stakes proportionally; leftovers go to the last share."""
    total = sum(stakes)
    if pot <= 0 or total <= 0:
        return [0 for _ in stakes]
    shares = []
    remaining = pot
    for i, stake in enumerate(stakes):
        if i == len(stakes) - 1:
            shares.append(remaining)
        else:
            share = (stake * pot) // total
            shares.append(share)
            remaining -= share
    return shares


async def settle_pari_mutuel_bets(
    conn,
    bets,
    *,
    winner_key: str,
    predicted_field: str,
    table: str,
    winner_label: str,
    loser_label: str,
) -> list[str]:
    """
    Settle spectator/team bets as a closed pool:
    losers fund winners proportionally; no mint/burn beyond the book.
    """
    if not bets:
        return []

    winners = [b for b in bets if b[predicted_field] == winner_key]
    losers = [b for b in bets if b[predicted_field] != winner_key]
    lines: list[str] = []

    if not winners:
        for bet in bets:
            bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
            await release_escrow(
                conn,
                int(bet["bettor_id"]),
                bet["amount"],
                f"{table} bet voided — no winning tickets (match book refund)",
                fmt_user(bettor),
            )
            await conn.execute(
                f"UPDATE {table} SET status = 'REFUNDED', payout_amount = 0 WHERE bet_id = $1",
                bet["bet_id"],
            )
            lines.append(f"  ↩️ {fmt_user(bettor)} bet {fmt(bet['amount'])} → Refunded (no winning side)")
        return lines

    lose_total = sum(int(b["amount"]) for b in losers)
    shares = _pari_mutuel_shares([int(b["amount"]) for b in winners], lose_total)

    for bet in losers:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        await burn_escrow(
            conn,
            int(bet["bettor_id"]),
            bet["amount"],
            f"{table} bet lost (pari-mutuel)",
            fmt_user(bettor),
        )
        await conn.execute(
            f"UPDATE {table} SET status = 'LOST', payout_amount = 0 WHERE bet_id = $1",
            bet["bet_id"],
        )
        lines.append(
            f"  ❌ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{loser_label}** → Lost"
        )

    for bet, share in zip(winners, shares):
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        await release_escrow(
            conn,
            int(bet["bettor_id"]),
            bet["amount"],
            f"{table} bet won (escrow released)",
            fmt_user(bettor),
        )
        if share > 0:
            await credit(
                conn,
                int(bet["bettor_id"]),
                share,
                f"{table} bet won (pari-mutuel share)",
                fmt_user(bettor),
            )
        await conn.execute(
            f"UPDATE {table} SET status = 'WON', payout_amount = $1 WHERE bet_id = $2",
            share,
            bet["bet_id"],
        )
        if share > 0:
            lines.append(
                f"  ✅ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{winner_label}** → Won {fmt(share)}"
            )
        else:
            lines.append(
                f"  ✅ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{winner_label}** → Stake returned (no opposing pool)"
            )

    return lines


async def propose_or_confirm_winner(
    conn,
    match_id: str,
    winner_id: str,
    reporter_id: str,
) -> tuple[str, asyncpg.Record | None]:
    """
    Dual-confirm winner reporting for player matches.
    Returns (result, completed_row_or_None) where result is one of:
    proposed | updated | waiting | disputed | confirmed
    """
    match = await lock_match(conn, match_id)
    if not match:
        raise ValueError("missing_match")
    if match["status"] != "ACTIVE":
        raise ValueError(f"bad_status:{match['status']}")
    if winner_id not in (match["challenger_id"], match["opponent_id"]):
        raise ValueError("bad_winner")
    if reporter_id not in (match["challenger_id"], match["opponent_id"]):
        raise ValueError("bad_reporter")

    proposed = match["proposed_winner_id"]
    reported_by = match["reported_by_id"]

    if (
        proposed
        and reported_by
        and reported_by != reporter_id
        and proposed == winner_id
    ):
        completed = await complete_match_if_active(conn, match_id, winner_id, reporter_id)
        if not completed:
            raise ValueError("race_completed")
        await conn.execute(
            "UPDATE matches SET proposed_winner_id = NULL WHERE match_id = $1",
            match_id,
        )
        return "confirmed", completed

    if proposed == winner_id and reported_by == reporter_id:
        return "waiting", None

    await conn.execute(
        """
        UPDATE matches
        SET proposed_winner_id = $1, reported_by_id = $2
        WHERE match_id = $3 AND status = 'ACTIVE'
        """,
        winner_id,
        reporter_id,
        match_id,
    )
    if proposed and reported_by and reported_by != reporter_id and proposed != winner_id:
        return "disputed", None
    if proposed and reported_by == reporter_id and proposed != winner_id:
        return "updated", None
    return "proposed", None


async def run_payout(conn, match_id: str, winner_id: str, mod_tag: str | None = None) -> discord.Embed:
    match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    wager = match["wager_amount"]
    challenger_id = match["challenger_id"]
    opponent_id = match["opponent_id"]
    loser_id = challenger_id if winner_id == opponent_id else opponent_id

    winner_user = await get_bot().fetch_user(int(winner_id))
    loser_user = await get_bot().fetch_user(int(loser_id))

    bets = await conn.fetch("SELECT * FROM bets WHERE match_id = $1 AND status = 'PENDING'", match_id)
    await lock_users_ordered(
        conn,
        int(winner_id),
        int(loser_id),
        *[int(b["bettor_id"]) for b in bets],
    )

    # Player battle wager stays zero-sum between the two players.
    await release_escrow(conn, int(winner_id), wager, "battle win escrow released", fmt_user(winner_user))
    await credit(conn, int(winner_id), wager, "battle win (opponent wager)", fmt_user(winner_user))
    await burn_escrow(conn, int(loser_id), wager, "battle loss", fmt_user(loser_user))

    bet_lines = await settle_pari_mutuel_bets(
        conn,
        bets,
        winner_key=str(winner_id),
        predicted_field="predicted_winner_id",
        table="bets",
        winner_label=fmt_user(winner_user),
        loser_label=fmt_user(loser_user),
    )

    await conn.execute(
        "UPDATE matches SET proposed_winner_id = NULL WHERE match_id = $1",
        match_id,
    )

    embed = discord.Embed(title="⚔️ Match Complete!", color=discord.Color.gold())
    embed.add_field(name="Match ID", value=match_id, inline=True)
    embed.add_field(name="Winner", value=winner_user.mention, inline=True)
    embed.add_field(name="Payout", value=fmt(wager * 2), inline=True)
    if bet_lines:
        text = "\n".join(bet_lines)
        if len(text) > 1000:
            text = text[:1000] + "\n…"
        embed.add_field(name="Bet Outcomes (pari-mutuel)", value=text, inline=False)
    if mod_tag:
        embed.set_footer(text=f"Force-resolved by mod: {mod_tag}")

    await log(
        f"🏆 MATCH COMPLETED — Match ID: {match_id} | Winner: {fmt_user(winner_user)} | Loser: {fmt_user(loser_user)} | Wager: {fmt(wager)}"
        + (f" | Force-resolved by: {mod_tag}" if mod_tag else "")
    )
    return embed

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



async def _role_mention(role_id):
    from bot_presentation import get_role_mention
    return await get_role_mention(role_id)


async def lock_team_match(conn, match_id: str):
    from bot_presentation import lock_team_match as _lock
    return await _lock(conn, match_id)

async def run_team_match_payout(conn, match_id: str, winner_role_id: str, mod_tag: str | None = None) -> discord.Embed:
    match = await conn.fetchrow("SELECT * FROM team_matches WHERE match_id = $1", match_id)
    role_one_id = match["role_one_id"]
    role_two_id = match["role_two_id"]
    loser_role_id = role_two_id if winner_role_id == role_one_id else role_one_id

    bets = await conn.fetch(
        "SELECT * FROM team_bets WHERE match_id = $1 AND status = 'PENDING'",
        match_id,
    )
    await lock_users_ordered(conn, *[int(b["bettor_id"]) for b in bets])

    bet_lines = await settle_pari_mutuel_bets(
        conn,
        bets,
        winner_key=str(winner_role_id),
        predicted_field="predicted_role_id",
        table="team_bets",
        winner_label=await _role_mention(winner_role_id),
        loser_label=await _role_mention(loser_role_id),
    )

    embed = discord.Embed(title="🏟️ Team Match Complete!", color=discord.Color.gold())
    embed.add_field(name="Match ID", value=match_id, inline=True)
    embed.add_field(name="Winner", value=await _role_mention(winner_role_id), inline=True)
    if bet_lines:
        text = "\n".join(bet_lines)
        if len(text) > 1000:
            text = text[:1000] + "\n…"
        embed.add_field(name="Bet Outcomes (pari-mutuel)", value=text, inline=False)
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


async def restore_team_match_to_pre_payout(conn, match_id: str, previous_winner_role_id: str):
    """Reverse a completed team match payout so it can be re-resolved."""
    match = await lock_team_match(conn, match_id)
    if not match:
        raise PayoutReverseError(f"Team match `{match_id}` not found.")
    if match["status"] != "COMPLETED":
        raise PayoutReverseError(
            f"Team match `{match_id}` must be COMPLETED to reverse a payout (current: {match['status']})."
        )

    bets = await conn.fetch("SELECT * FROM team_bets WHERE match_id = $1", match_id)
    locked = await lock_users_ordered(conn, *[int(b["bettor_id"]) for b in bets])

    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        bettor_row = locked.get(int(bet["bettor_id"]))
        if bet["status"] == "WON":
            payout = int(bet["payout_amount"] or 0)
            if not bettor_row or bettor_row["balance"] < payout:
                have = bettor_row["balance"] if bettor_row else 0
                raise PayoutReverseError(
                    f"Cannot reverse team match `{match_id}`: {fmt_user(bettor)} only has {fmt(have)} "
                    f"but needs {fmt(payout)} still available to return bet winnings."
                )
            if bettor_row["balance"] - payout < bettor_row["escrow"] + bet["amount"]:
                raise PayoutReverseError(
                    f"Cannot reverse team match `{match_id}`: {fmt_user(bettor)} cannot re-escrow "
                    f"{fmt(bet['amount'])} after returning {fmt(payout)} winnings."
                )
        elif bet["status"] == "REFUNDED":
            avail = spendable(bettor_row) if bettor_row else 0
            if not bettor_row or avail < bet["amount"]:
                raise PayoutReverseError(
                    f"Cannot reverse team match `{match_id}`: {fmt_user(bettor)} only has {fmt(avail)} "
                    f"available to re-escrow a previously refunded bet of {fmt(bet['amount'])}."
                )

    for bet in bets:
        bettor = await get_bot().fetch_user(int(bet["bettor_id"]))
        if bet["status"] == "WON":
            payout = int(bet["payout_amount"] or 0)
            updated = await conn.fetchrow(
                """
                UPDATE users
                SET balance = balance - $1, escrow = escrow + $2
                WHERE user_id = $3
                  AND balance >= $1
                  AND balance - $1 >= escrow + $2
                RETURNING user_id
                """,
                payout,
                bet["amount"],
                bet["bettor_id"],
            )
            if not updated:
                raise PayoutReverseError(
                    f"Cannot reverse team match `{match_id}`: failed to reclaim bet winnings from {fmt_user(bettor)}."
                )
            await log(
                f"↩️ TEAM BET PAYOUT REVERSED — {fmt_user(bettor)} returned {fmt(payout)} winnings "
                f"and had {fmt(bet['amount'])} re-escrowed (match {match_id})"
            )
        elif bet["status"] == "LOST":
            await conn.execute(
                "UPDATE users SET balance = balance + $1, escrow = escrow + $1 WHERE user_id = $2",
                bet["amount"],
                bet["bettor_id"],
            )
            await log(
                f"↩️ TEAM BET PAYOUT REVERSED — {fmt_user(bettor)} had {fmt(bet['amount'])} restored "
                f"and re-escrowed (match {match_id})"
            )
        elif bet["status"] == "REFUNDED":
            await debit_escrow(
                conn,
                int(bet["bettor_id"]),
                bet["amount"],
                f"team bet re-escrowed after reverse (match {match_id})",
                fmt_user(bettor),
            )
            await log(
                f"↩️ TEAM BET REFUND REVERSED — {fmt_user(bettor)} re-escrowed {fmt(bet['amount'])} (match {match_id})"
            )

    await conn.execute(
        """
        UPDATE team_bets
        SET status = 'PENDING', payout_amount = 0
        WHERE match_id = $1 AND status IN ('WON', 'LOST', 'REFUNDED')
        """,
        match_id,
    )
    await log(
        f"↩️ TEAM MATCH PAYOUT REVERSED — Match ID: {match_id} | Previous winner role: {previous_winner_role_id}"
    )
