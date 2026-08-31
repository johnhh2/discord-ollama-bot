import time
import datetime
import logging
from zoneinfo import ZoneInfo

import discord

from src import state
from src.config import OLLAMA_MODEL, DAILY_RESET_HOUR, LOTTERY_SEED_POOL
from src.persistence import (
    save_economy, save_insurance, try_set_record,
    load_balance_history, save_balance_history,
    load_bot_stats_history, save_bot_stats_history,
    load_command_usage_history, save_command_usage_history,
    upsert_crime_delta, upsert_gambling_delta,
    prune_balance_history, prune_bot_stats_history, prune_command_usage_history,
    prune_crime_history, prune_gambling_history, prune_levelup_history,
    prune_notable_events, prune_daily_counters,
)
from src.guild_config import get_guild_cfg


async def _ensure_user(uid: int):
    # Block until init_db_state has loaded state from the DB. Without this,
    # any caller that lands here before the load finishes (background tasks
    # like the leveling voice tick, lottery draw, etc.) would materialize a
    # zero-valued entry and UPSERT it over the user's real DB row.
    import src.persistence as _pkg
    await _pkg.init_done.wait()
    key = str(uid)
    if key not in state.economy["users"]:
        state.economy["users"][key] = {"balance": 0, "last_daily": 0.0}
        await save_economy(uid=uid)


# Threshold above which a player becomes a valid crime target regardless
# of level. Combined wallet + (current value of) savings.
CRIME_ELIGIBLE_NET_WORTH = 100_000


async def _maybe_latch_crime_eligible(uid: int) -> bool:
    """Sticky: if the user qualifies (display level >= 10 in any guild, OR
    wallet + savings > 100k now), set the flag and persist. Returns True iff
    this call latched the flag (i.e. the user was previously ineligible and
    just qualified). The level-up branch of grant_xp also latches on the
    transition into display level 10."""
    user = state.economy["users"].get(str(uid))
    if user is None or user.get("crime_eligible"):
        return False
    wallet = user.get("balance", 0)
    # Compound value, not raw principal — the doc above and the user-facing
    # copy both say "current value of savings", and get_savings_value is the
    # same formula the rest of the economy uses.
    savings_total = await get_savings_value(uid)
    if wallet + savings_total > CRIME_ELIGIBLE_NET_WORTH:
        user["crime_eligible"] = True
        await save_economy(uid=uid)
        return True
    # Catch users who reached display level 10 (internal 9) before this latch
    # existed, or in any guild — grant_xp only fires on the level-up edge.
    ukey = str(uid)
    for guild_data in state.leveling.values():
        rec = guild_data.get(ukey)
        if rec and rec.get("level", 0) >= 9:
            user["crime_eligible"] = True
            await save_economy(uid=uid)
            return True
    return False


async def get_balance(uid: int) -> int:
    await _ensure_user(uid)
    return state.economy["users"][str(uid)]["balance"]


async def get_total_balance(uid: int) -> int:
    """Wallet + current savings value — the number the highest_balance record
    tracks. Announce sites must display this, not the bare wallet, so the
    announced value matches the record."""
    await _ensure_user(uid)
    wallet = state.economy["users"][str(uid)]["balance"]
    return wallet + int(await get_savings_value(uid))


async def add_balance(uid: int, n: int, guild_id: int = None, holder_name: str = None) -> bool:
    """Adds `n` to uid's balance. Returns True if this caused a new
    highest_balance record (wallet + savings)."""
    await _ensure_user(uid)
    state.economy["users"][str(uid)]["balance"] += n
    await save_economy(uid=uid)
    await _maybe_latch_crime_eligible(uid)
    if guild_id is not None and holder_name is not None:
        total = await get_total_balance(uid)
        return await try_set_record(guild_id, "highest_balance", total, uid, holder_name)
    return False


async def deduct_balance(uid: int, n: int) -> bool:
    await _ensure_user(uid)
    key = str(uid)
    if state.economy["users"][key]["balance"] < n:
        return False
    state.economy["users"][key]["balance"] -= n
    await save_economy(uid=uid)
    return True


def get_guild_house_balance(guild_id: int) -> int:
    return state.economy.get("guild_house", {}).get(str(guild_id), 0)


async def add_guild_house(guild_id: int, amount: int):
    from src.persistence import save_guild_house
    state.economy.setdefault("guild_house", {})
    key = str(guild_id)
    state.economy["guild_house"][key] = state.economy["guild_house"].get(key, 0) + amount
    await save_guild_house(guild_id)


async def drain_bot_balance_into_lottery(lottery: dict, guild_id: int) -> int:
    """Transfer this guild's house balance into the lottery prize pool. Returns the amount transferred."""
    from src.persistence import save_guild_house
    state.economy.setdefault("guild_house", {})
    key = str(guild_id)
    house_balance = state.economy["guild_house"].get(key, 0)
    if house_balance > 0:
        state.economy["guild_house"][key] = 0
        await save_guild_house(guild_id)
        lottery["prize_pool"] = lottery.get("prize_pool", 0) + house_balance
    return house_balance


async def announce_new_lottery(
    channel: discord.TextChannel, prize_pool: int = LOTTERY_SEED_POOL,
    now: datetime.datetime = None
):
    """Announce a new monthly lottery to the specified channel."""
    from src.helpers import C_PURPLE
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    ct = ZoneInfo("America/Chicago")
    now_cst = now.astimezone(ct)
    timestamp = int(next_lottery_draw_dt(now_cst).timestamp())

    embed = discord.Embed(title="🎰 New Monthly Lottery", color=C_PURPLE)
    embed.description = (
        "A new lottery has started! Grab your daily 🎟️ with `!lottery` "
        "or the dailies-channel 🎟️ button\n\n"
        f"**Prize Pool:** {prize_pool:,} 🪙 (+1,000 🪙 per player)\n"
        "**Tickets:** 1,000 🪙 for 1 🎟️, one per day per server — plus up to "
        "3 free 🎟️ a week from chess wins (any win 1, a 600+ Elo bot 2, 1100+ 3)\n"
        f"**Ends:** <t:{timestamp}:R>"
    )
    await channel.send(embed=embed)


# Everything a bought/renewed insurance day protects against. Single source
# of truth for `!shop insurance` and the subscription renewal below.
INSURANCE_PROTECTS = ["ragebait", "mock", "nickname", "role", "steal", "tax", "spellcheck"]


def extend_insurance(uid: int, days: int) -> int:
    """Extend the user's bot-wide insurance by `days` × 24h, from the current
    expiry when coverage is still active (so renewals keep coverage
    continuous), else from now. Synchronous on purpose — callers stamp before
    their first await (see CLAUDE.md on per-user command races). Returns the
    new expiry ts. Caller is responsible for save_insurance()."""
    from src.config import SHOP_INSURANCE_DURATION_SECS
    key = int(uid)
    now = time.time()
    existing = state.insurance.get(key)
    base = existing["expires_at"] if existing and existing.get("expires_at", 0) > now else now
    expires_at = int(base + days * SHOP_INSURANCE_DURATION_SECS)
    state.insurance[key] = {"expires_at": expires_at, "protected_from": list(INSURANCE_PROTECTS)}
    return expires_at


async def renew_insurance_subs(uid: int) -> tuple[int, int]:
    """Charge and renew `uid`'s insurance subscription, if they hold one.

    Called only from sweep_insurance_subs, inside the synchronously-claimed
    last_insurance_sweep window — that's what makes it once-per-gameplay-day.
    Deducts one day's premium and extends the bot-wide coverage by 24h. A
    renewal the user can't afford is skipped (coverage lapses until they can
    pay again); the subscription itself stays.

    Returns (total_charged, lapsed_count) — each 0 or one premium/lapse now
    that insurance is global, but callers still read both.
    """
    from src.config import SHOP_INSURANCE_COST
    if int(uid) not in state.insurance_subs:
        return 0, 0
    free = uid in state.godmode_users
    if free or await deduct_balance(uid, SHOP_INSURANCE_COST):
        extend_insurance(uid, 1)
        await save_insurance()
        return 0 if free else SHOP_INSURANCE_COST, 0
    return 0, 1


async def sweep_insurance_subs() -> None:
    """Charge every insurance subscriber one day's premium for the new
    gameplay-day — independent of whether they log on.

    Called from EconomyCog's minute loop. The last_insurance_sweep marker
    (persisted in economy_meta) makes it fire once per gameplay-day: at the
    first tick after the 5am CT rollover, or right after boot if the bot was
    down at 5am. A subscriber who can't afford the premium lapses for the day
    (coverage stops extending; the subscription stays). Charges and lapses
    are tallied into ins_paid_since_claim / ins_lapsed_since_claim, which the
    user's next daily claim reports and resets — the sweep itself sends
    nothing.

    On the very first run (no marker yet — the boot that ships this feature)
    the day is stamped without charging: subscribers were already charged by
    the old claim-time flow that day.
    """
    today = _ct_today()
    prior = state.economy.get("last_insurance_sweep")
    if prior == today:
        return
    # Claim the day synchronously before any await — a boot sweep and the
    # minute loop must not both charge (see CLAUDE.md on command races).
    state.economy["last_insurance_sweep"] = today
    from src.persistence import save_insurance_sweep_day
    await save_insurance_sweep_day()
    if prior is None:
        return
    for uid in list(state.insurance_subs):
        await _ensure_user(uid)
        charged, lapsed = await renew_insurance_subs(uid)
        user_data = state.economy["users"][str(uid)]
        if charged:
            user_data["ins_paid_since_claim"] = int(user_data.get("ins_paid_since_claim", 0) or 0) + charged
        if lapsed:
            user_data["ins_lapsed_since_claim"] = int(user_data.get("ins_lapsed_since_claim", 0) or 0) + lapsed
        if charged or lapsed:
            await save_economy(uid=uid)


async def is_insured(uid: int, against: str) -> bool:
    """True if user `uid` holds active insurance against `against`.

    Insurance is bot-wide (one policy per user, valid in every server).
    Expired entries are pruned on read.
    """
    key = int(uid)
    if key not in state.insurance:
        return False
    entry = state.insurance[key]
    if entry.get("expires_at", 0) <= time.time():
        del state.insurance[key]
        await save_insurance()
        return False
    return against in entry.get("protected_from", [])


def get_insurance_expiry(uid: int) -> int | None:
    """Return the user's insurance expiry timestamp, or None."""
    entry = state.insurance.get(int(uid))
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


def _ct_today_date() -> datetime.date:
    """Same as _ct_today() but returns a datetime.date instead of an ISO string.
    Convenience for graph code that needs to compare/append dates directly.
    """
    now_ct = _ct_now()
    if now_ct.hour < DAILY_RESET_HOUR:
        return now_ct.date() - datetime.timedelta(days=1)
    return now_ct.date()


def _calendar_today_date() -> datetime.date:
    """Calendar date in CT (rolls at midnight, NOT at the 5am gameplay
    boundary). Used by graph series whose disk rows are keyed by calendar
    date — crime, gambling, and level-ups all write `_ct_now().date()` on
    every event, so their graph build functions must read by calendar date
    too, otherwise the live-today bar would lag for 5 hours every morning.
    """
    return _ct_now().date()


# ── 6-hour bucket scheme ─────────────────────────────────────────────────────
# Each calendar day is split into 4 fixed CT buckets:
#   0 = 00:00–05:59 CT  (overnight)
#   1 = 06:00–11:59 CT  (morning)
#   2 = 12:00–17:59 CT  (afternoon)
#   3 = 18:00–23:59 CT  (evening)
# History tables key rows by (snapshot_date, bucket, ...), giving the graphs
# 4 data points per day. The 30-minute snapshot loop UPSERTs into the current
# bucket; once the bucket rolls over (every 6h CT), the next write creates a
# new row, freezing the previous bucket's value.

def _current_bucket_ct() -> int:
    """Return 0..3 based on the current CT hour."""
    return _ct_now().hour // 6


# How many days of graph history we retain on disk. The graph cog only
# reads the last 14 days for rendering today; the longer retention is
# headroom for future "show me 90 days" / "show me a year" features.
GRAPH_HISTORY_RETENTION_DAYS = 3650  # ~10 years


def _bucket_start_dt(date_iso: str, bucket: int) -> datetime.datetime:
    """Given a calendar-date string and a 0..3 bucket, return the UTC
    timestamp of the bucket's start (CT 00:00, 06:00, 12:00, or 18:00).
    Used by the graph cog as the x-axis position for each data point.
    """
    d = datetime.date.fromisoformat(date_iso)
    return datetime.datetime.combine(
        d, datetime.time(bucket * 6, 0), tzinfo=ZoneInfo("America/Chicago")
    ).astimezone(datetime.timezone.utc)


def lottery_month_key(now_ct: datetime.datetime) -> int:
    """Year-qualified month key: year * 100 + month (YYYYMM, e.g. 202608).

    The year qualifier keeps a stale last_drawn key from colliding with
    the same month a year later, mirroring the old YYYYWW week scheme.

    Transition from the weekly lottery (deployed 2026-07-31): the DB's
    last_drawn_week/last_posted_week columns still hold YYYYWW week keys
    from July 2026 (weeks 27-31, i.e. last two digits >= 27). Those can
    never equal a YYYYMM key (last two digits 01-12), so the first
    1st-of-month draw fires normally and overwrites them with month keys
    via the normal save path.
    """
    return now_ct.year * 100 + now_ct.month


def lottery_week_key() -> str:
    """ISO year-week key ("2026-W35") of the current gameplay-day in CT.

    Gates the free weekly chess-win lottery tickets. Derived from _ct_today()
    so the week rolls with the 5am gameplay-day boundary, not midnight.
    """
    iso = _ct_today_date().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def next_lottery_draw_dt(now_ct: datetime.datetime) -> datetime.datetime:
    """Next lottery draw: the upcoming 1st of the month at 6pm CT.

    Returns today 6pm if called on the 1st before the draw, otherwise
    the 1st of the following month.
    """
    ct = ZoneInfo("America/Chicago")
    this_month_draw = datetime.datetime(now_ct.year, now_ct.month, 1, 18, 0, tzinfo=ct)
    if now_ct < this_month_draw:
        return this_month_draw
    if now_ct.month == 12:
        return datetime.datetime(now_ct.year + 1, 1, 1, 18, 0, tzinfo=ct)
    return datetime.datetime(now_ct.year, now_ct.month + 1, 1, 18, 0, tzinfo=ct)


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


# ── Savings interest ─────────────────────────────────────────────────────────
# 0.6% compound per day — a bit over half the property revenue rate
# (1.1% of cost per day, see PROPERTY_DAILY_REVENUE_PERMILLE).
SAVINGS_DAILY_MULT = 1.006
# User-facing rate string — keeps help copy in lockstep with the math.
SAVINGS_DAILY_PCT = f"{(SAVINGS_DAILY_MULT - 1.0) * 100:.2f}%"
# Deposits accrued 1%/day before the 2026-08-26 rate cut. Interest earned
# before the changeover is kept: the growth factor switches rate at the
# boundary instead of recomputing history at the new rate.
_LEGACY_SAVINGS_DAILY_MULT = 1.01
SAVINGS_RATE_CHANGE_TS = 1787702400.0  # 2026-08-26 00:00:00 UTC


def savings_growth(deposited_at: float, now: float | None = None) -> float:
    """Compound growth factor for one savings deposit from deposited_at to now.
    The single formula for savings value — every read path must use it."""
    if now is None:
        now = time.time()
    if now <= deposited_at:
        return 1.0
    legacy_days = max(0.0, (min(now, SAVINGS_RATE_CHANGE_TS) - deposited_at) / 86400.0)
    new_days = max(0.0, (now - max(deposited_at, SAVINGS_RATE_CHANGE_TS)) / 86400.0)
    return (_LEGACY_SAVINGS_DAILY_MULT ** legacy_days) * (SAVINGS_DAILY_MULT ** new_days)


async def get_savings_value(uid: int) -> float:
    """Return the current compound value of a user's savings (savings_growth per deposit)."""
    await _ensure_user(uid)
    deposits = state.economy["users"][str(uid)].get("savings", [])
    now = time.time()
    total = 0.0
    for entry in deposits:
        total += entry["amount"] * savings_growth(entry["deposited_at"], now)
    return total


async def add_savings(uid: int, amount: int) -> bool:
    """Deduct amount from balance and add a new savings deposit. Returns False if insufficient funds."""
    if not await deduct_balance(uid, amount):
        return False
    await _ensure_user(uid)
    user = state.economy["users"][str(uid)]
    user.setdefault("savings", [])
    user["savings"].append({"amount": amount, "deposited_at": time.time()})
    await save_economy(uid=uid)
    await _maybe_latch_crime_eligible(uid)
    return True


async def remove_savings(uid: int, amount: int) -> bool:
    """Withdraw amount from savings back to balance. Returns False if savings value < amount."""
    await _ensure_user(uid)
    user = state.economy["users"][str(uid)]
    deposits = user.get("savings", [])
    now = time.time()
    current_value = int(sum(e["amount"] * savings_growth(e["deposited_at"], now) for e in deposits))
    if current_value < amount:
        return False
    remaining = float(amount)
    new_deposits = []
    for entry in deposits:
        factor = savings_growth(entry["deposited_at"], now)
        val = entry["amount"] * factor
        if remaining <= 0:
            new_deposits.append(entry)
        elif val <= remaining:
            remaining -= val
        else:
            # Store the leftover principal as a float so the kept value
            # (kept_amount * factor) exactly equals (val - remaining). Truncating
            # to int here used to lose up to ~factor coins, letting a withdraw +
            # redeposit of the same amount drop displayed savings by 1.
            kept_amount = entry["amount"] - remaining / factor
            if kept_amount > 0:
                new_deposits.append({"amount": kept_amount, "deposited_at": entry["deposited_at"]})
            remaining = 0
    user["savings"] = new_deposits
    await add_balance(uid, amount)
    await save_economy(uid=uid)
    return True


async def seize_from_savings(uid: int, max_amount: int) -> int:
    """Drain up to max_amount from savings without crediting the wallet.
    Returns the actual amount seized (0 if savings empty or max_amount <= 0)."""
    if max_amount <= 0:
        return 0
    await _ensure_user(uid)
    user = state.economy["users"][str(uid)]
    deposits = user.get("savings", [])
    now = time.time()
    current_value = int(sum(e["amount"] * savings_growth(e["deposited_at"], now) for e in deposits))
    if current_value <= 0:
        return 0
    seized = min(max_amount, current_value)
    remaining = float(seized)
    new_deposits = []
    for entry in deposits:
        factor = savings_growth(entry["deposited_at"], now)
        val = entry["amount"] * factor
        if remaining <= 0:
            new_deposits.append(entry)
        elif val <= remaining:
            remaining -= val
        else:
            kept_amount = entry["amount"] - remaining / factor
            if kept_amount > 0:
                new_deposits.append({"amount": kept_amount, "deposited_at": entry["deposited_at"]})
            remaining = 0
    user["savings"] = new_deposits
    await save_economy(uid=uid)
    return seized


async def snapshot_balances():
    """Record each user's wallet and savings value into the CURRENT 6h bucket
    of today's date. Called by the 30min snapshot loop — multiple ticks
    within the same bucket overwrite the same row; the bucket boundary
    creates a fresh row.

    Pruning lives in `do_daily_reset` (DB-level DELETE), not here — the
    snapshot loop runs every 30min and shouldn't churn through old rows."""
    import time as _time
    from src.properties import portfolio_value
    today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    history = await load_balance_history()
    snapshot = {}
    now = _time.time()
    for uid_str, user in state.economy["users"].items():
        wallet = user.get("balance", 0)
        deps = user.get("savings", [])
        savings = int(sum(e["amount"] * savings_growth(e["deposited_at"], now) for e in deps))
        snapshot[uid_str] = {
            "wallet": wallet, "savings": savings,
            # Property book value + lifetime revenue banked — feeds
            # !graph assets and the economy graph's Property segment.
            "assets": portfolio_value(int(uid_str)),
            "asset_revenue": int(user.get("property_revenue_total", 0) or 0),
        }
    history.setdefault(today, {})[bucket] = snapshot
    await save_balance_history(history)
    logging.info(f"[snapshot] balances for {today} bucket {bucket}: {len(snapshot)} users")


async def snapshot_bot_stats(ai_up: bool, ping_ms: float | None = None):
    """Write current message/command/AI counts, memory, and gateway ping
    into today's CURRENT 6h bucket. Pruning lives in do_daily_reset.

    `ping_ms` is None when the caller has no gateway latency to report
    (daily reset path, or pre-first-heartbeat) — the row keeps NULL and the
    next 30-minute tick refreshes it.
    """
    from src.helpers import get_memory_mb
    today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    history = await load_bot_stats_history()
    if ping_ms is None:
        # Keep a ping an earlier tick already recorded for this bucket.
        ping_ms = history.get(today, {}).get(bucket, {}).get("ping_ms")
    mc_up = state.mc_last_online
    mc_ping_ms = state.mc_last_ping_ms
    if mc_up is None:
        # No monitor sample yet (boot snapshot, or feature disabled) — keep
        # whatever an earlier tick recorded for this bucket.
        prev = history.get(today, {}).get(bucket, {})
        mc_up = prev.get("mc_up")
        mc_ping_ms = prev.get("mc_ping_ms")
    history.setdefault(today, {})[bucket] = {
        "messages": state.stats_messages_today,
        "commands": state.stats_commands_today,
        "ai_responses": state.stats_ai_responses_today,
        "ai_up": ai_up,
        "memory_mb": get_memory_mb(),
        "ping_ms": ping_ms,
        "mc_up": mc_up,
        "mc_ping_ms": mc_ping_ms,
    }
    await save_bot_stats_history(history)
    logging.info(f"[snapshot] bot stats for {today} bucket {bucket}: {history[today][bucket]}")


async def snapshot_command_usage():
    """Write current per-cog command counts into today's CURRENT 6h bucket.
    Pruning lives in do_daily_reset."""
    today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    history = await load_command_usage_history()
    history.setdefault(today, {})[bucket] = dict(state.stats_commands_today_by_cog)
    await save_command_usage_history(history)
    logging.info(f"[snapshot] per-cog command usage for {today} bucket {bucket}: {history[today][bucket]}")


async def record_crime_event(guild_id: int, uid: int, *, gained: int = 0, lost: int = 0):
    """Atomically write the current bucket's crime delta for `uid` in
    `guild_id` to crime_history AND bump the in-memory cache for the same
    bucket.

    Called by !steal / !mug on every outcome (win/lose, attacker/victim).
    Persists synchronously — no data loss on bot restart. If the 6h CT
    bucket has rolled over since the last recorded event, the in-memory
    cache is cleared first so it only reflects the current bucket. A
    falsy `guild_id` (DM context — shouldn't happen for crime) is a no-op.
    """
    if gained == 0 and lost == 0 or not guild_id:
        return
    today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    if state._crime_bucket != bucket:
        state.crime_today_by_user.clear()
        state._crime_bucket = bucket
    await upsert_crime_delta(today, bucket, guild_id, uid, gained=gained, lost=lost)
    rec = state.crime_today_by_user.setdefault((int(guild_id), str(uid)), {"gained": 0, "lost": 0})
    rec["gained"] += int(gained)
    rec["lost"] += int(lost)


async def record_gambling_event(guild_id: int, uid: int, *, gained: int = 0, lost: int = 0):
    """Atomically write the current bucket's gambling delta for `uid` in
    `guild_id` to gambling_history AND bump the in-memory cache.

    Called by games/gambling commands at outcome resolution (net P/L
    semantics: refunds and pushes record nothing). Persists synchronously.
    Bucket rollover is detected and the cache is reset accordingly. A
    falsy `guild_id` (DM context) is a no-op — gambling P/L is per-server.
    """
    if gained == 0 and lost == 0 or not guild_id:
        return
    today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    if state._gambling_bucket != bucket:
        state.gambling_today_by_user.clear()
        state._gambling_bucket = bucket
    await upsert_gambling_delta(today, bucket, guild_id, uid, gained=gained, lost=lost)
    rec = state.gambling_today_by_user.setdefault((int(guild_id), str(uid)), {"gained": 0, "lost": 0})
    rec["gained"] += int(gained)
    rec["lost"] += int(lost)


async def snapshot_all(ping_ms: float | None = None):
    """Run the periodic graph-data snapshots. Called by the GraphCog scheduler
    every 6 hours and once on boot.

    `ping_ms` is the gateway heartbeat latency, passed in by the GraphCog
    loop (the only caller with a bot reference). The daily-reset path omits
    it; that bucket's ping refreshes on the next scheduler tick.

    Note: crime, gambling, and level-ups are NOT snapshotted here — they're
    written atomically at event time via the `record_*_event` helpers, so the
    *_history tables are always current. Only the aggregate counters
    (balances, bot stats, per-cog command usage) need periodic flushing.
    """
    from src.ai import check_ollama_connected
    await snapshot_balances()
    ai_up = await check_ollama_connected()
    await snapshot_bot_stats(ai_up, ping_ms=ping_ms)
    await snapshot_command_usage()


async def do_daily_reset():
    """Reset all users' daily reward and scratchoff counts at 5am CT.

    Captures a final snapshot of yesterday's aggregate counters BEFORE clearing
    them — the 6h GraphCog scheduler can't be relied on to fire exactly at 5am,
    so without this the last hours of the gameplay-day would be lost.

    The crime/gambling/levelup dicts are NOT cleared here — they're keyed
    by calendar date on the disk side and persisted atomically per-event,
    so they roll over naturally at calendar midnight (not gameplay 5am).
    """
    today = _ct_today()
    await snapshot_all()
    state.stats_messages_today = 0
    state.stats_commands_today = 0
    state.stats_ai_responses_today = 0
    state.stats_commands_today_by_cog.clear()
    for user in state.economy["users"].values():
        user["daily_date"] = None
        user["scratch_used"] = 0
        user["scratch_date"] = today
        user["scratch_won_today"] = 0
        user["jailbreak_used"] = False
        user["bot_chess_elo_max_today"] = 0
        user["bot_chess_elo_max_date"] = today
    state.economy["last_daily_reset"] = today
    await save_economy()

    # Prune all graph-history tables once per gameplay-day via a single
    # DB-level DELETE per table — cheap, correct, and uniform across the
    # snapshot-style and atomic-write tables.
    cutoff = (_ct_now().date() - datetime.timedelta(days=GRAPH_HISTORY_RETENTION_DAYS)).isoformat()
    await prune_balance_history(before_date=cutoff)
    await prune_bot_stats_history(before_date=cutoff)
    await prune_command_usage_history(before_date=cutoff)
    await prune_crime_history(before_date=cutoff)
    await prune_gambling_history(before_date=cutoff)
    await prune_levelup_history(before_date=cutoff)
    await prune_notable_events(before_date=cutoff)
    await prune_daily_counters(before_date=cutoff)

    logging.info(f"[DAILY] Reset daily reward and scratchoff counts for {today}")
