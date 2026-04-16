import os
import asyncio
import asyncpg
import discord
from discord.ext import commands
from discord import option
import random
import string
from datetime import datetime, timezone, timedelta
import traceback

# ─────────────────────────────────────────────
# CONFIGURATION VARIABLES
# ─────────────────────────────────────────────
CURRENCY_NAME = "Wok"
CURRENCY_SYMBOL = "元"
DAILY_AMOUNT = 50
STARTING_BALANCE = 0
MIN_BATTLE_WAGER = 100
CHALLENGE_TIMEOUT_SECONDS = 300
MODERATOR_ROLE_ID = 1494455406691483658
LOG_CHANNEL_ID = 1494449437240463451
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
bot = discord.Bot(intents=intents)
db_pool: asyncpg.Pool = None


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def fmt(amount: int) -> str:
    return f"{CURRENCY_SYMBOL}{amount}"

def gen_id(length=5) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def fmt_user(user: discord.User | discord.Member) -> str:
    return f"{user.name}#{user.discriminator}" if user.discriminator != "0" else user.name

def ts() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")


async def log(message: str):
    """Send a message to the log channel."""
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
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
        str(user_id), STARTING_BALANCE
    )


async def get_user(conn, user_id: int) -> asyncpg.Record:
    return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", str(user_id))


async def spendable(record: asyncpg.Record) -> int:
    return record["balance"] - record["escrow"]


async def credit(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
        amount, str(user_id)
    )
    row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", str(user_id))
    await log(f"💰 BALANCE CREDITED — {user_tag or user_id} received {fmt(amount)} ({reason}) | New balance: {fmt(row['balance'])}")


async def debit_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET escrow = escrow + $1 WHERE user_id = $2",
        amount, str(user_id)
    )
    row = await conn.fetchrow("SELECT balance, escrow FROM users WHERE user_id = $1", str(user_id))
    await log(f"🔒 ESCROW HELD — {user_tag or user_id} escrowed {fmt(amount)} ({reason}) | Available: {fmt(row['balance'] - row['escrow'])}")


async def release_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    await conn.execute(
        "UPDATE users SET escrow = escrow - $1 WHERE user_id = $2",
        amount, str(user_id)
    )
    await log(f"🔓 ESCROW RELEASED — {user_tag or user_id} refunded {fmt(amount)} ({reason})")


async def burn_escrow(conn, user_id: int, amount: int, reason: str, user_tag: str = ""):
    """Deduct escrow AND balance (lose the funds entirely)."""
    await conn.execute(
        "UPDATE users SET balance = balance - $1, escrow = escrow - $1 WHERE user_id = $2",
        amount, str(user_id)
    )
    await log(f"🔥 ESCROW BURNED — {user_tag or user_id} lost {fmt(amount)} ({reason})")


def has_mod_role(ctx: discord.ApplicationContext) -> bool:
    if isinstance(ctx.author, discord.Member):
        return any(r.id == MODERATOR_ROLE_ID for r in ctx.author.roles)
    return False


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                escrow INTEGER NOT NULL DEFAULT 0,
                last_daily TIMESTAMPTZ
            )
        """)
        await conn.execute("""
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
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                bet_id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL REFERENCES matches(match_id),
                bettor_id TEXT NOT NULL,
                predicted_winner_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
            )
        """)
    print(f"✅ Logged in as {bot.user} | DB connected")
    await log(f"🤖 Bot started and ready — {bot.user}")


# ─────────────────────────────────────────────
# PAYOUT LOGIC (shared by /report and /resolve)
# ─────────────────────────────────────────────
async def run_payout(conn, match_id: str, winner_id: str, channel: discord.TextChannel, mod_tag: str = None):
    match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    wager = match["wager_amount"]
    challenger_id = match["challenger_id"]
    opponent_id = match["opponent_id"]
    loser_id = challenger_id if winner_id == opponent_id else opponent_id

    winner_user = await bot.fetch_user(int(winner_id))
    loser_user = await bot.fetch_user(int(loser_id))

    # Winner: release escrow + credit loser's wager
    await release_escrow(conn, int(winner_id), wager, "battle win escrow released", fmt_user(winner_user))
    await credit(conn, int(winner_id), wager, "battle win (opponent wager)", fmt_user(winner_user))

    # Loser: burn their escrow
    await burn_escrow(conn, int(loser_id), wager, "battle loss", fmt_user(loser_user))

    # Process bets
    bets = await conn.fetch("SELECT * FROM bets WHERE match_id = $1 AND status = 'PENDING'", match_id)
    bet_lines = []
    for bet in bets:
        bettor = await bot.fetch_user(int(bet["bettor_id"]))
        if bet["predicted_winner_id"] == winner_id:
            await release_escrow(conn, int(bet["bettor_id"]), bet["amount"], "bet won (escrow released)", fmt_user(bettor))
            await credit(conn, int(bet["bettor_id"]), bet["amount"], "bet won (winnings)", fmt_user(bettor))
            await conn.execute("UPDATE bets SET status = 'WON' WHERE bet_id = $1", bet["bet_id"])
            bet_lines.append(f"  ✅ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{fmt_user(winner_user)}** → Won {fmt(bet['amount'])}")
        else:
            await burn_escrow(conn, int(bet["bettor_id"]), bet["amount"], "bet lost", fmt_user(bettor))
            await conn.execute("UPDATE bets SET status = 'LOST' WHERE bet_id = $1", bet["bet_id"])
            bet_lines.append(f"  ❌ {fmt_user(bettor)} bet {fmt(bet['amount'])} on **{fmt_user(loser_user)}** → Lost")

    # Post results
    embed = discord.Embed(title="⚔️ Match Complete!", color=discord.Color.gold())
    embed.add_field(name="Match ID", value=match_id, inline=True)
    embed.add_field(name="Winner", value=winner_user.mention, inline=True)
    embed.add_field(name="Payout", value=fmt(wager * 2), inline=True)
    if bet_lines:
        embed.add_field(name="Bet Outcomes", value="\n".join(bet_lines), inline=False)
    if mod_tag:
        embed.set_footer(text=f"Force-resolved by mod: {mod_tag}")
    await channel.send(embed=embed)

    await log(f"🏆 MATCH COMPLETED — Match ID: {match_id} | Winner: {fmt_user(winner_user)} | Loser: {fmt_user(loser_user)} | Wager: {fmt(wager)}" + (f" | Force-resolved by: {mod_tag}" if mod_tag else ""))


# ─────────────────────────────────────────────
# CHALLENGE BUTTONS VIEW
# ─────────────────────────────────────────────
class ChallengeView(discord.ui.View):
    def __init__(self, match_id: str, challenger_id: int, opponent_id: int):
        super().__init__(timeout=CHALLENGE_TIMEOUT_SECONDS)
        self.match_id = match_id
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id

    async def on_timeout(self):
        async with db_pool.acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match or match["status"] != "PENDING":
                return
            await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", self.match_id)
            challenger = await bot.fetch_user(self.challenger_id)
            await release_escrow(conn, self.challenger_id, match["wager_amount"], "battle timeout refund", fmt_user(challenger))
            channel = bot.get_channel(int(match["channel_id"]))
            if channel and match["message_id"]:
                try:
                    msg = await channel.fetch_message(int(match["message_id"]))
                    embed = discord.Embed(
                        title="⚔️ Challenge Expired",
                        description=f"The challenge timed out. {challenger.mention}'s wager has been refunded.",
                        color=discord.Color.dark_gray()
                    )
                    await msg.edit(embed=embed, view=None)
                except Exception:
                    pass
            await log(f"⏰ BATTLE TIMEOUT — Match ID: {self.match_id} | Challenger: {fmt_user(challenger)} | Wager refunded: {fmt(match['wager_amount'])}")

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message("This is not your battle to accept.", ephemeral=True)
        async with db_pool.acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match or match["status"] != "PENDING":
                return await interaction.response.send_message("This challenge is no longer valid.", ephemeral=True)

            opponent = interaction.user
            await ensure_user(conn, opponent.id)
            opp_row = await get_user(conn, opponent.id)
            avail = await spendable(opp_row)
            if avail < match["wager_amount"]:
                return await interaction.response.send_message(
                    f"You don't have enough {CURRENCY_NAME} to accept. You need {fmt(match['wager_amount'])} but only have {fmt(avail)} available.",
                    ephemeral=True
                )

            await debit_escrow(conn, opponent.id, match["wager_amount"], f"battle wager escrowed (match {self.match_id})", fmt_user(opponent))
            await conn.execute(
                "UPDATE matches SET status = 'ACCEPTED', accepted_at = $1 WHERE match_id = $2",
                now_utc(), self.match_id
            )

        challenger = await bot.fetch_user(self.challenger_id)
        embed = discord.Embed(
            title="⚔️ Challenge Accepted!",
            description=f"{challenger.mention} vs {opponent.mention}",
            color=discord.Color.green()
        )
        embed.add_field(name="Wager", value=fmt(match["wager_amount"]), inline=True)
        embed.add_field(name="Match ID", value=self.match_id, inline=True)
        embed.add_field(name="Status", value="Bets are now open! Use `/bet` to wager on a player.\nWhen ready, either player can use `/start " + self.match_id + "` to begin.", inline=False)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)
        await log(f"✅ BATTLE ACCEPTED — Match ID: {self.match_id} | {fmt_user(challenger)} vs {fmt_user(opponent)} | Wager: {fmt(match['wager_amount'])}")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message("This is not your battle to decline.", ephemeral=True)
        async with db_pool.acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", self.match_id)
            if not match or match["status"] != "PENDING":
                return await interaction.response.send_message("This challenge is no longer valid.", ephemeral=True)
            challenger = await bot.fetch_user(self.challenger_id)
            await release_escrow(conn, self.challenger_id, match["wager_amount"], "battle declined refund", fmt_user(challenger))
            await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", self.match_id)

        embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"{interaction.user.mention} declined the battle. Wager refunded.",
            color=discord.Color.red()
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)
        await log(f"❌ BATTLE DECLINED — Match ID: {self.match_id} | Declined by: {fmt_user(interaction.user)} | Wager refunded: {fmt(match['wager_amount'])}")


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────

@bot.slash_command(description="Challenge another player to a Wok battle")
@option("opponent", discord.Member, description="The player you want to challenge")
@option("amount", int, description="Wager amount in Wok")
async def battle(ctx: discord.ApplicationContext, opponent: discord.Member, amount: int):
    try:
        await ctx.defer()
        if amount < MIN_BATTLE_WAGER:
            return await ctx.respond(f"The minimum battle wager is {fmt(MIN_BATTLE_WAGER)}.", ephemeral=True)
        if opponent.id == ctx.author.id:
            return await ctx.respond("You cannot battle yourself.", ephemeral=True)
        if opponent.bot:
            return await ctx.respond("You cannot battle a bot.", ephemeral=True)

        async with db_pool.acquire() as conn:
            await ensure_user(conn, ctx.author.id)
            await ensure_user(conn, opponent.id)
            challenger_row = await get_user(conn, ctx.author.id)
            opponent_row = await get_user(conn, opponent.id)

            if await spendable(challenger_row) < amount:
                return await ctx.respond(f"You have insufficient funds. You need {fmt(amount)} but only have {fmt(await spendable(challenger_row))} available.", ephemeral=True)
            if await spendable(opponent_row) < amount:
                return await ctx.respond(f"{opponent.display_name} doesn't have enough {CURRENCY_NAME} to match that wager.", ephemeral=True)

            match_id = gen_id()
            while await conn.fetchrow("SELECT 1 FROM matches WHERE match_id = $1", match_id):
                match_id = gen_id()

            await debit_escrow(conn, ctx.author.id, amount, f"battle wager escrowed (match {match_id})", fmt_user(ctx.author))
            await conn.execute(
                """
                INSERT INTO matches (match_id, challenger_id, opponent_id, wager_amount, status, channel_id, created_at)
                VALUES ($1, $2, $3, $4, 'PENDING', $5, $6)
                """,
                match_id, str(ctx.author.id), str(opponent.id), amount, str(ctx.channel_id), now_utc()
            )

        embed = discord.Embed(title="⚔️ Battle Challenge!", color=discord.Color.orange())
        embed.add_field(name="Challenger", value=ctx.author.mention, inline=True)
        embed.add_field(name="Opponent", value=opponent.mention, inline=True)
        embed.add_field(name="Wager", value=fmt(amount), inline=True)
        embed.add_field(name="Match ID", value=match_id, inline=True)
        embed.set_footer(text=f"Challenge expires in {CHALLENGE_TIMEOUT_SECONDS // 60} minutes")

        view = ChallengeView(match_id, ctx.author.id, opponent.id)
        msg = await ctx.respond(embed=embed, view=view)
        fetched = await msg.original_response()
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE matches SET message_id = $1 WHERE match_id = $2", str(fetched.id), match_id)

        await log(f"⚔️ BATTLE CREATED — Challenger: {fmt_user(ctx.author)} vs Opponent: {fmt_user(opponent)} | Wager: {fmt(amount)} | Match ID: {match_id}")

    except Exception as e:
        await ctx.respond("Something went wrong. Please try again.", ephemeral=True)
        await log(f"❌ ERROR — Command: /battle | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="Start a battle (locks bets)")
@option("match_id", str, description="The match ID to start")
async def start(ctx: discord.ApplicationContext, match_id: str):
    try:
        match_id = match_id.upper()
        async with db_pool.acquire() as conn:
            await ensure_user(conn, ctx.author.id)
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
            if not match:
                return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
            if str(ctx.author.id) not in (match["challenger_id"], match["opponent_id"]):
                return await ctx.respond("You are not a participant in this match.", ephemeral=True)
            if match["status"] != "ACCEPTED":
                return await ctx.respond(f"Match `{match_id}` is not in ACCEPTED status (current: {match['status']}).", ephemeral=True)
            await conn.execute(
                "UPDATE matches SET status = 'ACTIVE', started_at = $1 WHERE match_id = $2",
                now_utc(), match_id
            )

        embed = discord.Embed(
            title="🥊 Match Started!",
            description=f"Match `{match_id}` is now **ACTIVE**. Bets are locked. Fight!",
            color=discord.Color.blue()
        )
        await ctx.respond(embed=embed)
        await log(f"🥊 MATCH STARTED — Match ID: {match_id} | Started by: {fmt_user(ctx.author)}")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /start | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="Report the winner of your match")
@option("match_id", str, description="The match ID")
@option("winner", discord.Member, description="The winner of the match")
async def report(ctx: discord.ApplicationContext, match_id: str, winner: discord.Member):
    try:
        match_id = match_id.upper()
        async with db_pool.acquire() as conn:
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
                str(winner.id), str(ctx.author.id), match_id
            )
            await ctx.defer()
            await run_payout(conn, match_id, str(winner.id), ctx.channel)
            await ctx.respond(f"Match `{match_id}` has been completed!", ephemeral=True)

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /report | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="Bet on a player in an ongoing match")
@option("match_id", str, description="The match ID to bet on")
@option("player", discord.Member, description="The player you're betting on")
@option("amount", int, description="Amount to bet")
async def bet(ctx: discord.ApplicationContext, match_id: str, player: discord.Member, amount: int):
    try:
        match_id = match_id.upper()
        if amount <= 0:
            return await ctx.respond("Bet amount must be greater than zero.", ephemeral=True)

        async with db_pool.acquire() as conn:
            await ensure_user(conn, ctx.author.id)
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
            if not match:
                return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
            if str(ctx.author.id) in (match["challenger_id"], match["opponent_id"]):
                return await ctx.respond("Players cannot bet on their own match.", ephemeral=True)
            if match["status"] != "ACCEPTED":
                return await ctx.respond(f"Bets are only open during ACCEPTED status (current: {match['status']}).", ephemeral=True)
            if str(player.id) not in (match["challenger_id"], match["opponent_id"]):
                return await ctx.respond("You must bet on one of the two players.", ephemeral=True)

            existing = await conn.fetchrow(
                "SELECT 1 FROM bets WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'",
                match_id, str(ctx.author.id)
            )
            if existing:
                return await ctx.respond("You already have an active bet on this match. Use `/cancelbet` to change it.", ephemeral=True)

            bettor_row = await get_user(conn, ctx.author.id)
            if await spendable(bettor_row) < amount:
                return await ctx.respond(f"Insufficient funds. You have {fmt(await spendable(bettor_row))} available.", ephemeral=True)

            bet_id = gen_id(5)
            while await conn.fetchrow("SELECT 1 FROM bets WHERE bet_id = $1", bet_id):
                bet_id = gen_id(5)

            await debit_escrow(conn, ctx.author.id, amount, f"bet escrowed (match {match_id})", fmt_user(ctx.author))
            await conn.execute(
                "INSERT INTO bets (bet_id, match_id, bettor_id, predicted_winner_id, amount, status) VALUES ($1, $2, $3, $4, $5, 'PENDING')",
                bet_id, match_id, str(ctx.author.id), str(player.id), amount
            )

        embed = discord.Embed(
            title="🎲 Bet Placed!",
            description=f"{ctx.author.mention} bet {fmt(amount)} on {player.mention} in match `{match_id}`",
            color=discord.Color.purple()
        )
        await ctx.respond(embed=embed)
        await log(f"🎲 BET PLACED — {fmt_user(ctx.author)} bet {fmt(amount)} on {fmt_user(player)} | Match: {match_id} | Bet ID: {bet_id}")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /bet | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="Check your balance or another user's balance")
@option("user", discord.Member, description="User to check (defaults to you)", required=False)
async def balance(ctx: discord.ApplicationContext, user: discord.Member = None):
    try:
        target = user or ctx.author
        async with db_pool.acquire() as conn:
            await ensure_user(conn, target.id)
            row = await get_user(conn, target.id)

        avail = row["balance"] - row["escrow"]
        embed = discord.Embed(title=f"💰 {target.display_name}'s Balance", color=discord.Color.green())
        embed.add_field(name="Available", value=fmt(avail), inline=True)
        embed.add_field(name="In Escrow", value=fmt(row["escrow"]), inline=True)
        embed.add_field(name="Total", value=fmt(row["balance"]), inline=True)
        await ctx.respond(embed=embed)

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /balance | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description=f"Claim your daily {fmt(DAILY_AMOUNT)} {CURRENCY_NAME}")
async def daily(ctx: discord.ApplicationContext):
    try:
        async with db_pool.acquire() as conn:
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
                        ephemeral=True
                    )

            await conn.execute(
                "UPDATE users SET balance = balance + $1, last_daily = $2 WHERE user_id = $3",
                DAILY_AMOUNT, now, str(ctx.author.id)
            )
            updated = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", str(ctx.author.id))

        embed = discord.Embed(
            title="☀️ Daily Claimed!",
            description=f"You received {fmt(DAILY_AMOUNT)} {CURRENCY_NAME}!\nNew balance: {fmt(updated['balance'])}",
            color=discord.Color.yellow()
        )
        await ctx.respond(embed=embed)
        await log(f"☀️ DAILY CLAIMED — {fmt_user(ctx.author)} received {fmt(DAILY_AMOUNT)} | New balance: {fmt(updated['balance'])}")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /daily | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="Cancel a battle you created (before it's accepted)")
@option("match_id", str, description="The match ID to cancel")
async def cancelbattle(ctx: discord.ApplicationContext, match_id: str):
    try:
        match_id = match_id.upper()
        async with db_pool.acquire() as conn:
            await ensure_user(conn, ctx.author.id)
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
            if not match:
                return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
            if match["challenger_id"] != str(ctx.author.id):
                return await ctx.respond("Only the challenger can cancel a battle.", ephemeral=True)
            if match["status"] != "PENDING":
                return await ctx.respond(f"You can only cancel a PENDING match (current: {match['status']}).", ephemeral=True)

            await conn.execute("UPDATE matches SET status = 'CANCELLED' WHERE match_id = $1", match_id)
            await release_escrow(conn, ctx.author.id, match["wager_amount"], f"battle cancelled by challenger (match {match_id})", fmt_user(ctx.author))

            channel = bot.get_channel(int(match["channel_id"]))
            if channel and match["message_id"]:
                try:
                    msg = await channel.fetch_message(int(match["message_id"]))
                    embed = discord.Embed(
                        title="❌ Battle Cancelled",
                        description=f"{ctx.author.mention} cancelled the challenge. Wager refunded.",
                        color=discord.Color.dark_gray()
                    )
                    await msg.edit(embed=embed, view=None)
                except Exception:
                    pass

        await ctx.respond(f"Battle `{match_id}` cancelled and your wager of {fmt(match['wager_amount'])} has been refunded.", ephemeral=True)
        await log(f"🚫 BATTLE CANCELLED — Match ID: {match_id} | Cancelled by: {fmt_user(ctx.author)} | Wager refunded: {fmt(match['wager_amount'])}")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /cancelbattle | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="Cancel your bet on a match (before it starts)")
@option("match_id", str, description="The match ID your bet is on")
async def cancelbet(ctx: discord.ApplicationContext, match_id: str):
    try:
        match_id = match_id.upper()
        async with db_pool.acquire() as conn:
            await ensure_user(conn, ctx.author.id)
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
            if not match:
                return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
            if match["status"] != "ACCEPTED":
                return await ctx.respond(f"Bets can only be cancelled while match is ACCEPTED (current: {match['status']}).", ephemeral=True)

            existing_bet = await conn.fetchrow(
                "SELECT * FROM bets WHERE match_id = $1 AND bettor_id = $2 AND status = 'PENDING'",
                match_id, str(ctx.author.id)
            )
            if not existing_bet:
                return await ctx.respond(f"You don't have an active bet on match `{match_id}`.", ephemeral=True)

            await release_escrow(conn, ctx.author.id, existing_bet["amount"], f"bet cancelled (match {match_id})", fmt_user(ctx.author))
            await conn.execute("DELETE FROM bets WHERE bet_id = $1", existing_bet["bet_id"])

        await ctx.respond(f"Your bet of {fmt(existing_bet['amount'])} on match `{match_id}` has been cancelled and refunded.", ephemeral=True)
        await log(f"🚫 BET CANCELLED — {fmt_user(ctx.author)} cancelled bet of {fmt(existing_bet['amount'])} on match {match_id} | Refunded")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /cancelbet | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="[MOD] Adjust a user's balance")
@option("user", discord.Member, description="User to adjust")
@option("amount", int, description="Amount to add (positive) or remove (negative)")
async def adjustbalance(ctx: discord.ApplicationContext, user: discord.Member, amount: int):
    try:
        if not has_mod_role(ctx):
            return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

        async with db_pool.acquire() as conn:
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
            ephemeral=True
        )
        await log(f"🔧 BALANCE ADJUSTED — Mod: {fmt_user(ctx.author)} | User: {fmt_user(user)} | Change: {fmt(actual_change)}{note} | New balance: {fmt(new_balance)}")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /adjustbalance | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


@bot.slash_command(description="[MOD] Force-resolve a match")
@option("match_id", str, description="The match ID to resolve")
@option("winner", discord.Member, description="Declare the winner")
async def resolve(ctx: discord.ApplicationContext, match_id: str, winner: discord.Member):
    try:
        if not has_mod_role(ctx):
            return await ctx.respond("You don't have permission to use this command.", ephemeral=True)

        match_id = match_id.upper()
        async with db_pool.acquire() as conn:
            match = await conn.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
            if not match:
                return await ctx.respond(f"Match `{match_id}` not found.", ephemeral=True)
            if str(winner.id) not in (match["challenger_id"], match["opponent_id"]):
                return await ctx.respond("The winner must be one of the two players in this match.", ephemeral=True)

            await conn.execute(
                "UPDATE matches SET status = 'COMPLETED', winner_id = $1, reported_by_id = $2 WHERE match_id = $3",
                str(winner.id), str(ctx.author.id), match_id
            )
            await ctx.defer()
            await run_payout(conn, match_id, str(winner.id), ctx.channel, mod_tag=fmt_user(ctx.author))

        await ctx.respond(f"Match `{match_id}` has been force-resolved.", ephemeral=True)
        await log(f"🔧 MATCH FORCE-RESOLVED — Match ID: {match_id} | Mod: {fmt_user(ctx.author)} | Winner: {fmt_user(winner)}")

    except Exception as e:
        await ctx.respond("Something went wrong.", ephemeral=True)
        await log(f"❌ ERROR — Command: /resolve | User: {fmt_user(ctx.author)} | Error: {traceback.format_exc()}")


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
bot.run(DISCORD_TOKEN)