import time
import datetime
import logging
from zoneinfo import ZoneInfo

import discord

from src.config import OLLAMA_MODEL, DAILY_RESET_HOUR
from src.persistence import (
    save_economy, save_insurance, get_guild_cfg, try_set_record,
    load_balance_history, save_balance_history,
    load_bot_stats_history, save_bot_stats_history,
)


def _ensure_user(uid: int):
    from src import state
    key = str(uid)
    if key not in state.economy["users"]:
        state.economy["users"][key] = {"balance": 0, "last_daily": 0.0}
        save_economy()


def get_balance(uid: int) -> int:
    from src import state
    _ensure_user(uid)
    return state.economy["users"][str(uid)]["balance"]


def add_balance(uid: int, n: int, guild_id: int = None, holder_name: str = None):
    from src import state
    _ensure_user(uid)
    state.economy["users"][str(uid)]["balance"] += n
    save_economy()
    if guild_id is not None and holder_name is not None:
        new_bal = state.economy["users"][str(uid)]["balance"]
        try_set_record(guild_id, "highest_balance", new_bal, uid, holder_name)


def deduct_balance(uid: int, n: int) -> bool:
    from src import state
    _ensure_user(uid)
    key = str(uid)
    if state.economy["users"][key]["balance"] < n:
        return False
    state.economy["users"][key]["balance"] -= n
    save_economy()
    return True


def get_guild_house_balance(guild_id: int) -> int:
    from src import state
    return state.economy.get("guild_house", {}).get(str(guild_id), 0)


def add_guild_house(guild_id: int, amount: int):
    from src import state
    state.economy.setdefault("guild_house", {})
    key = str(guild_id)
    state.economy["guild_house"][key] = state.economy["guild_house"].get(key, 0) + amount
    save_economy()


def drain_bot_balance_into_lottery(lottery: dict, guild_id: int) -> int:
    """Transfer this guild's house balance into the lottery prize pool. Returns the amount transferred."""
    from src import state
    state.economy.setdefault("guild_house", {})
    key = str(guild_id)
    house_balance = state.economy["guild_house"].get(key, 0)
    if house_balance > 0:
        state.economy["guild_house"][key] = 0
        save_economy()
        lottery["prize_pool"] = lottery.get("prize_pool", 0) + house_balance
    return house_balance


async def announce_new_lottery(
    channel: discord.TextChannel, prize_pool: int = 2000,
    now: datetime.datetime = None
):
    """Announce a new lottery week to the specified channel."""
    from src.helpers import C_PURPLE
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    ct = ZoneInfo("America/Chicago")
    now_cst = now.astimezone(ct)
    days_until_saturday = (5 - now_cst.weekday()) % 7
    next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
    next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
    if next_saturday <= now_cst:
        next_saturday += datetime.timedelta(weeks=1)
    timestamp = int(next_saturday.timestamp())

    embed = discord.Embed(title="🎰 New Lottery Week", color=C_PURPLE)
    embed.description = (
        "A new lottery has started! Buy tickets with `!lottery <n>`\n\n"
        f"**Prize Pool:** {prize_pool:,} 🪙 (+1,000 🪙 per player)\n"
        f"**Ticket Cost:** 10 🪙 for 1 🎟️\n"
        f"**Ends:** <t:{timestamp}:R>"
    )
    await channel.send(embed=embed)


def is_insured(uid: int, against: str) -> bool:
    import src as _src
    insurance = _src.insurance
    if str(uid) not in insurance:
        return False
    entry = insurance[str(uid)]
    if entry.get("expires_at", 0) <= time.time():
        del insurance[str(uid)]
        save_insurance()
        return False
    return against in entry.get("protected_from", [])


def get_insurance_expiry(uid: int) -> int | None:
    """Return the insurance expiry timestamp for uid, or None if not insured."""
    import src as _src
    entry = _src.insurance.get(str(uid))
    if entry and entry.get("expires_at", 0) > time.time():
        return int(entry["expires_at"])
    return None


def get_guild_ask_model(guild_id: int) -> str:
    cfg = get_guild_cfg(guild_id)
    return cfg.get("ask_model", OLLAMA_MODEL)


def get_guild_roleplay_model(guild_id: int) -> str:
    cfg = get_guild_cfg(guild_id)
    return cfg.get("roleplay_model", OLLAMA_MODEL)


def get_guild_coding_model(guild_id: int) -> str:
    cfg = get_guild_cfg(guild_id)
    return cfg.get("coding_model", OLLAMA_MODEL)


def _ct_now() -> datetime.datetime:
    """Return the current time in America/Chicago timezone."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("America/Chicago"))


def _ct_today() -> str:
    """Return the current 'day' in CT, where a new day starts at 5am CT."""
    now_ct = _ct_now()
    if now_ct.hour < DAILY_RESET_HOUR:
        return (now_ct.date() - datetime.timedelta(days=1)).isoformat()
    return now_ct.date().isoformat()


def next_daily_reset_ts() -> int:
    """Unix timestamp of the next 5am CT daily reset."""
    now_ct = _ct_now()
    if now_ct.hour < DAILY_RESET_HOUR:
        reset_date = now_ct.date()
    else:
        reset_date = now_ct.date() + datetime.timedelta(days=1)
    reset_dt = datetime.datetime.combine(
        reset_date,
        datetime.time(DAILY_RESET_HOUR, 0),
        tzinfo=ZoneInfo("America/Chicago"),
    )
    return int(reset_dt.timestamp())


def get_savings_value(uid: int) -> float:
    """Return the current compound value of a user's savings (1% daily interest per deposit)."""
    from src import state
    _ensure_user(uid)
    deposits = state.economy["users"][str(uid)].get("savings", [])
    now = time.time()
    total = 0.0
    for entry in deposits:
        days = (now - entry["deposited_at"]) / 86400.0
        total += entry["amount"] * (1.01 ** days)
    return total


def add_savings(uid: int, amount: int) -> bool:
    """Deduct amount from balance and add a new savings deposit. Returns False if insufficient funds."""
    from src import state
    if not deduct_balance(uid, amount):
        return False
    _ensure_user(uid)
    user = state.economy["users"][str(uid)]
    user.setdefault("savings", [])
    user["savings"].append({"amount": amount, "deposited_at": time.time()})
    save_economy()
    return True


def remove_savings(uid: int, amount: int) -> bool:
    """Withdraw amount from savings back to balance. Returns False if savings value < amount."""
    from src import state
    _ensure_user(uid)
    user = state.economy["users"][str(uid)]
    deposits = user.get("savings", [])
    now = time.time()
    current_value = int(sum(e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0)) for e in deposits))
    if current_value < amount:
        return False
    # Drain deposits from oldest first until we've covered the amount
    remaining = float(amount)
    new_deposits = []
    for entry in deposits:
        days = (now - entry["deposited_at"]) / 86400.0
        val = entry["amount"] * (1.01 ** days)
        if remaining <= 0:
            new_deposits.append(entry)
        elif val <= remaining:
            remaining -= val
        else:
            # Partial: compute what original principal fraction covers `remaining`
            # remaining = frac * val => frac = remaining / val
            frac = remaining / val
            kept_amount = int(entry["amount"] * (1 - frac))
            if kept_amount > 0:
                new_deposits.append({"amount": kept_amount, "deposited_at": entry["deposited_at"]})
            remaining = 0
    user["savings"] = new_deposits
    add_balance(uid, amount)
    save_economy()
    return True


def snapshot_balances():
    """Record each user's wallet and savings value keyed by today's date; prune entries older than 14 days."""
    from src import state
    import time as _time
    today = _ct_now().date().isoformat()
    history = load_balance_history()
    snapshot = {}
    now = _time.time()
    for uid_str, user in state.economy["users"].items():
        wallet = user.get("balance", 0)
        deps = user.get("savings", [])
        savings = int(sum(e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0)) for e in deps))
        snapshot[uid_str] = {"wallet": wallet, "savings": savings}
    history[today] = snapshot
    # Prune entries older than 14 days
    cutoff = (_ct_now().date() - datetime.timedelta(days=14)).isoformat()
    history = {d: v for d, v in history.items() if d >= cutoff}
    save_balance_history(history)
    logging.info(f"[DAILY] Snapshotted {len(snapshot)} user balances for {today}")


async def snapshot_bot_stats(ai_up: bool):
    """Record today's message/command/AI counts and memory; prune entries older than 14 days."""
    from src import state
    from src.helpers import get_memory_mb
    today = _ct_now().date().isoformat()
    history = load_bot_stats_history()
    history[today] = {
        "messages": state.stats_messages_today,
        "commands": state.stats_commands_today,
        "ai_responses": state.stats_ai_responses_today,
        "ai_up": ai_up,
        "memory_mb": get_memory_mb(),
    }
    cutoff = (_ct_now().date() - datetime.timedelta(days=14)).isoformat()
    history = {d: v for d, v in history.items() if d >= cutoff}
    save_bot_stats_history(history)
    logging.info(f"[DAILY] Snapshotted bot stats for {today}: {history[today]}")


async def do_daily_reset():
    """Reset all users' daily reward and scratchoff counts at 5am CT."""
    from src import state
    from src.ai import check_ollama_connected
    today = _ct_now().date().isoformat()
    snapshot_balances()
    ai_up = await check_ollama_connected()
    await snapshot_bot_stats(ai_up)
    # Reset today counters
    state.stats_messages_today = 0
    state.stats_commands_today = 0
    state.stats_ai_responses_today = 0
    for user in state.economy["users"].values():
        user["daily_date"] = None
        user["scratch_used"] = 0
        user["scratch_date"] = today
        user["jailbreak_used"] = False
    state.economy["last_daily_reset"] = today
    save_economy()
    logging.info(f"[DAILY] Reset daily reward and scratchoff counts for {today}")
