"""
Leveling system — domain logic
==============================
XP sources and caps:
  - Non-command message : 10 XP,  max 1 grant/hour,  max 5 grants/day
  - Command             :  5 XP,  max 1 grant/hour,  max 5 grants/day
  - Voice activity      :  5 XP,  max 1 grant/15 min, max 32 grants/day

Level curve: total XP required for level N = 100 * N * (N+1) / 2
  Level 1 → 100 XP total
  Level 2 → 300 XP total
  Level 3 → 600 XP total  (each level costs 100*(level) more than the last)

This module contains only pure domain functions. The Discord cog (transport)
lives in src/cogs/leveling_cog.py.
"""
import time

from src.persistence import save_leveling
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
VOICE_30MIN_MAX   = 1
VOICE_DAILY_MAX   = 16
STREAM_HOURLY_MAX = 1
STREAM_DAILY_MAX  = 3

HOUR_SECS   = 3600
MINS30_SECS = 1800
MINS15_SECS =  900
DAY_SECS    = 86400


# ── Level math ────────────────────────────────────────────────────────────────
# XP cost to go from level n to level n+1: 50 + n^1.9
# Total XP to reach level n = sum(50 + i^1.9 for i in range(n))

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
    """Reset daily counter if it's a new calendar day (UTC)."""
    now = time.time()
    last = rec.get(key_day_ts, 0.0)
    import datetime
    if datetime.datetime.utcfromtimestamp(now).date() > datetime.datetime.utcfromtimestamp(last).date():
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
    await save_leveling(guild_id=guild_id, uid=uid)
    return xp, leveled_up


# ── XP bar renderer ───────────────────────────────────────────────────────────

def _bar(filled: int, total: int, width: int = 20) -> str:
    done = round(filled / total * width) if total else 0
    done = max(0, min(width, done))
    return "█" * done + "░" * (width - done)
