"""
Leveling system — domain logic
==============================
XP sources and caps (source of truth: the constants below — keep in sync):
  - Non-command message : 10 XP,  max 1 grant/hour,   max  5 grants/day
  - Command             :  5 XP,  max 1 grant/hour,   max  5 grants/day
  - Voice activity      : 10 XP,  max 1 grant/30 min, max 16 grants/day
  - Stream              : 15 XP,  max 1 grant/hour,   max  3 grants/day
  - Scratchoff          :  5 XP per card played (3/day)

Level curve: advancing from level n to n+1 costs 100 + n^1.9 * 2 XP
(see _xp_cost below). Daily caps reset at 5am CT with the bot day.

This module contains only pure domain functions. The Discord cog (transport)
lives in src/cogs/leveling_cog.py.
"""
import time

from src.persistence import save_leveling, upsert_levelup_delta
from src import state

# ── XP constants ──────────────────────────────────────────────────────────────
XP_MESSAGE   = 10
XP_COMMAND   =  5
XP_VOICE     = 10
XP_SCRATCH   =  5
XP_STREAM    = 15

MSG_HOURLY_MAX    = 1
MSG_DAILY_MAX     = 5
CMD_HOURLY_MAX    = 1
CMD_DAILY_MAX     = 5
VOICE_DAILY_MAX   = 16
STREAM_HOURLY_MAX = 1
STREAM_DAILY_MAX  = 3

HOUR_SECS   = 3600
MINS30_SECS = 1800
DAY_SECS    = 86400


# ── Level math ────────────────────────────────────────────────────────────────

def _xp_cost(n: int) -> int:
    """XP required to advance from level n to level n+1."""
    return int(100 + n ** 1.9 * 2)


def xp_for_level(n: int) -> int:
    """Total XP required to *reach* level n (level 0 = 0 XP)."""
    return sum(_xp_cost(i) for i in range(n))


def level_from_xp(xp: int) -> int:
    """Level a user is at given total XP."""
    if xp <= 0:
        return 0
    level = 0
    total = 0
    while True:
        cost = _xp_cost(level)
        if total + cost > xp:
            return level
        total += cost
        level += 1


def xp_for_next_level(level: int) -> int:
    """Total XP required to reach level+1."""
    return xp_for_level(level + 1)


def display_level(internal_level: int) -> int:
    """Convert 0-based internal level to the 1-based level shown to users."""
    return internal_level + 1


_LEVEL_REWARD_TIERS = ((4, 0), (9, 1), (29, 2), (59, 3), (99, 4), (149, 5))


def levelup_coin_reward(display_lvl: int) -> int:
    """Coins awarded on reaching a given display level."""
    tier = next((t for max_lvl, t in _LEVEL_REWARD_TIERS if display_lvl <= max_lvl), 6)
    return 500 * (2 ** tier)


# ── User record helpers ───────────────────────────────────────────────────────
# Storage layout: state.leveling = {guild_id_str: {uid_str: {...}}}

def _ensure_lvl_record(guild_id: int, uid: int) -> dict:
    gkey = str(guild_id)
    ukey = str(uid)
    guild_data = state.leveling.setdefault(gkey, {})
    if ukey not in guild_data:
        guild_data[ukey] = {
            "xp": 0,
            "level": 0,
            # hourly / daily rate-limit tracking
            "msg_last_hour": 0.0,   # epoch of last msg XP grant
            "msg_today": 0,         # grants this calendar day
            "msg_day_ts": 0.0,      # epoch when msg_today was last reset
            "cmd_last_hour": 0.0,
            "cmd_today": 0,
            "cmd_day_ts": 0.0,
            "voice_last_30": 0.0,   # epoch of last voice XP grant
            "voice_today": 0,
            "voice_day_ts": 0.0,
            "stream_last_hour": 0.0,
            "stream_today": 0,
            "stream_day_ts": 0.0,
        }
    return guild_data[ukey]


def _day_reset(rec: dict, key_today: str, key_day_ts: str):
    """Reset daily counter when the 5am-CT bot day rolls over.

    Uses the economy's `_ct_today` day key (day boundary = 5am CT) so XP caps
    reset at the same moment as everything else. The old UTC-midnight logic
    reset at 6/7pm CT while !lvl displayed the 5am reset time.
    """
    from zoneinfo import ZoneInfo
    import datetime

    def _day_key(ts: float) -> str:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).astimezone(ZoneInfo("America/Chicago"))
        return (dt - datetime.timedelta(hours=5)).date().isoformat()

    now = time.time()
    last = rec.get(key_day_ts, 0.0)
    if _day_key(now) > _day_key(last):
        rec[key_today] = 0
        rec[key_day_ts] = now


async def grant_xp(uid: int, source: str, bot=None, guild_id: int = None) -> tuple[int, bool]:
    """
    Attempt to grant XP for *source* ('msg', 'cmd', 'voice').

    Returns (xp_granted, leveled_up).
    Does nothing and returns (0, False) if rate-limited or no guild_id.
    """
    if not guild_id:
        return 0, False
    # Block until init_db_state has loaded state from the DB. Without this,
    # a background tick (e.g. the voice loop firing on bot restart) would
    # materialize a zero-valued leveling rec and UPSERT it over the real row.
    import src.persistence as _pkg
    await _pkg.init_done.wait()
    rec = _ensure_lvl_record(guild_id, uid)
    now = time.time()

    if source == "msg":
        _day_reset(rec, "msg_today", "msg_day_ts")
        if now - rec["msg_last_hour"] < HOUR_SECS:
            return 0, False
        if rec["msg_today"] >= MSG_DAILY_MAX:
            return 0, False
        rec["msg_last_hour"] = now
        rec["msg_today"] += 1
        xp = XP_MESSAGE

    elif source == "cmd":
        _day_reset(rec, "cmd_today", "cmd_day_ts")
        if now - rec["cmd_last_hour"] < HOUR_SECS:
            return 0, False
        if rec["cmd_today"] >= CMD_DAILY_MAX:
            return 0, False
        rec["cmd_last_hour"] = now
        rec["cmd_today"] += 1
        xp = XP_COMMAND

    elif source == "voice":
        _day_reset(rec, "voice_today", "voice_day_ts")
        if now - rec.get("voice_last_30", 0.0) < MINS30_SECS:
            return 0, False
        if rec["voice_today"] >= VOICE_DAILY_MAX:
            return 0, False
        rec["voice_last_30"] = now
        rec["voice_today"] += 1
        xp = XP_VOICE

    elif source == "scratch":
        xp = XP_SCRATCH  # capped naturally by the scratchoff system (3/day)

    elif source == "stream":
        rec.setdefault("stream_last_hour", 0.0)
        rec.setdefault("stream_today", 0)
        rec.setdefault("stream_day_ts", 0.0)
        _day_reset(rec, "stream_today", "stream_day_ts")
        if now - rec["stream_last_hour"] < HOUR_SECS:
            return 0, False
        if rec["stream_today"] >= STREAM_DAILY_MAX:
            return 0, False
        rec["stream_last_hour"] = now
        rec["stream_today"] += 1
        xp = XP_STREAM

    else:
        return 0, False

    old_level = rec["level"]
    rec["xp"] += xp
    new_level = level_from_xp(rec["xp"])
    rec["level"] = new_level
    leveled_up = new_level > old_level
    if leveled_up:
        # A single grant can cross multiple thresholds — record each crossing.
        await record_levelup(guild_id, uid, count=new_level - old_level)
        # Latch crime eligibility once the user reaches display level 10
        # (internal level 9). Sticky: the flag never clears once set.
        if old_level < 9 <= new_level:
            from src.economy import _ensure_user as _eu
            from src import state as _state
            from src.persistence import save_economy as _save
            await _eu(uid)
            user = _state.economy["users"][str(uid)]
            if not user.get("crime_eligible"):
                user["crime_eligible"] = True
                await _save(uid=uid)
    await save_leveling(guild_id=guild_id, uid=uid)
    return xp, leveled_up


async def record_levelup(guild_id: int, uid: int, *, count: int = 1):
    """Atomically write the current bucket's level-up delta for (guild, user)
    to levelup_history AND bump the in-memory cache. Called by grant_xp on
    every level boundary crossing — multi-step grants pass count > 1.

    Bucket rollover (every 6h CT) is detected and the cache is reset.
    """
    if count <= 0:
        return
    from src.economy import _ct_now, _current_bucket_ct
    today = _ct_now().date().isoformat()
    bucket = _current_bucket_ct()
    if state._levelups_bucket != bucket:
        state.levelups_today.clear()
        state._levelups_bucket = bucket
    await upsert_levelup_delta(today, bucket, int(guild_id), int(uid), count=int(count))
    key = (int(guild_id), str(uid))
    state.levelups_today[key] = state.levelups_today.get(key, 0) + int(count)


# ── XP bar renderer ───────────────────────────────────────────────────────────

def _bar(filled: int, total: int, width: int = 20) -> str:
    done = round(filled / total * width) if total else 0
    done = max(0, min(width, done))
    return "█" * done + "░" * (width - done)
