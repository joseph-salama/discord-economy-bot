"""Database schema migrations."""
from bot_runtime import log, now_utc


async def init_database(conn):
    """Apply schema migrations in order. Safe to call on every startup."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    applied = {
        row["id"]
        for row in await conn.fetch("SELECT id FROM schema_migrations")
    }
    for migration_id, apply in _SCHEMA_MIGRATIONS:
        if migration_id in applied:
            continue
        async with conn.transaction():
            await apply(conn)
            await conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES ($1, $2)",
                migration_id,
                now_utc(),
            )
        await log(f"🧩 SCHEMA MIGRATION APPLIED — {migration_id}")


async def _migration_001_baseline(conn):
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            escrow INTEGER NOT NULL DEFAULT 0,
            last_daily TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
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
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            bet_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL REFERENCES matches(match_id),
            bettor_id TEXT NOT NULL,
            predicted_winner_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rewarded_queue_messages (
            message_id TEXT PRIMARY KEY,
            rewarded_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_matches (
            match_id TEXT PRIMARY KEY,
            role_one_id TEXT NOT NULL,
            role_two_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            winner_role_id TEXT,
            created_by_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_bets (
            bet_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL REFERENCES team_matches(match_id),
            bettor_id TEXT NOT NULL,
            predicted_role_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS bets_one_pending_per_user
        ON bets (match_id, bettor_id)
        WHERE status = 'PENDING'
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS team_bets_one_pending_per_user
        ON team_bets (match_id, bettor_id)
        WHERE status = 'PENDING'
        """
    )


async def _migration_002_constraints_and_columns(conn):
    # Clamp bad rows before adding CHECK so existing DBs can migrate cleanly.
    await conn.execute("UPDATE users SET escrow = 0 WHERE escrow < 0")
    await conn.execute("UPDATE users SET escrow = balance WHERE escrow > balance")
    await conn.execute(
        """
        DO $$ BEGIN
            ALTER TABLE users
            ADD CONSTRAINT users_balance_escrow_check
            CHECK (balance >= escrow AND escrow >= 0);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    await conn.execute(
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS proposed_winner_id TEXT"
    )
    await conn.execute(
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS payout_amount INTEGER NOT NULL DEFAULT 0"
    )
    await conn.execute(
        "ALTER TABLE team_bets ADD COLUMN IF NOT EXISTS payout_amount INTEGER NOT NULL DEFAULT 0"
    )
    # Old even-money wins stored no payout_amount; treat historical WON as 1:1.
    await conn.execute(
        """
        UPDATE bets
        SET payout_amount = amount
        WHERE status = 'WON' AND payout_amount = 0
        """
    )
    await conn.execute(
        """
        UPDATE team_bets
        SET payout_amount = amount
        WHERE status = 'WON' AND payout_amount = 0
        """
    )


_SCHEMA_MIGRATIONS = (
    ("001_baseline", _migration_001_baseline),
    ("002_constraints_and_columns", _migration_002_constraints_and_columns),
)
