import os

CURRENCY_NAME = "Dollars"
CURRENCY_SYMBOL = "$"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return int(str(raw).strip())


def _env_int_set(name: str, default: set[int]) -> set[int]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return set(default)
    return {int(part.strip()) for part in str(raw).split(",") if part.strip()}


ALLOWED_CHANNEL_ID = _env_int("ALLOWED_CHANNEL_ID", 1494473065281617971)
DAILY_AMOUNT = 50
STARTING_BALANCE = 250
MIN_BATTLE_WAGER = 100
MIN_TEAM_BET = 1
CHALLENGE_TIMEOUT_SECONDS = 300
TEAM_MATCH_OPEN_TIMEOUT_SECONDS = 7200  # 2 hours — OPEN team matches auto-cancel + refund
MODERATOR_ROLE_ID = _env_int("MODERATOR_ROLE_ID", 1494455406691483658)
LOG_CHANNEL_ID = _env_int("LOG_CHANNEL_ID", 1494449437240463451)
QUEUE_CHANNEL_IDS = _env_int_set(
    "QUEUE_CHANNEL_IDS",
    {1478102174541025451, 989621653703098398},
)
MATCH_REWARD = 100
TOP_PAGE_SIZE = 8
