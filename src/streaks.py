"""Sequential-day streak: any successful command or a click on the dailies
embed (DailiesCog.on_raw_reaction_add) counts.

Bumped the first time a user does either in a CT day (5am rollover, same day
definition as `_ct_today`). Distinct from the gambler streak in
src/gambling/scratchoff.py, which only counts full scratchoff days.
"""
import datetime

# Module (not name) import so the conftest save-fn stubs apply at call time.
from src import persistence, state
from src.helpers import announce_record


def get_command_streak_entry(uid_key: str) -> dict:
    """Return the streak entry as a dict {date, count} ({None, 0} if absent)."""
    entry = state.command_streak.get(uid_key)
    if entry is None:
        return {"date": None, "count": 0}
    return {"date": entry.get("date"), "count": int(entry.get("count", 1))}


def effective_streak(entry: dict, today_ct: str) -> int:
    """Current live streak count. A streak last bumped yesterday still counts
    (the user has until the end of today to keep it); anything older is 0."""
    if not entry or not entry.get("date"):
        return 0
    yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()
    if entry["date"] in (today_ct, yesterday):
        return int(entry.get("count", 0))
    return 0


async def update_command_streak(uid: int, today_ct: str) -> int:
    """Bump the user's daily streak. Returns the new count.

    Idempotent within a day: the first bump of the day extends (or resets)
    the streak and persists; every later one is a no-op read.
    """
    uid_key = str(uid)
    entry = get_command_streak_entry(uid_key)
    if entry["date"] == today_ct:
        return entry["count"]
    yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()
    new_count = entry["count"] + 1 if entry["date"] == yesterday else 1
    state.command_streak[uid_key] = {"date": today_ct, "count": new_count}
    await persistence.save_command_streak(uid)
    return new_count


async def bump_streak(uid: int, display_name: str, guild_id: int | None, channel, today_ct: str) -> int:
    """Count one qualifying action (a completed command or a dailies click)
    toward the user's daily streak and return the live count.

    Only the day's first bump can set the guild's longest-streak record, so
    the records table is consulted only then, and the record is announced
    in `channel`, where the action happened. `today_ct` is passed in (not
    computed here) so callers keep their own patched `_ct_today` in tests.
    """
    # Read the stored date BEFORE the bump to tell an actual extension from
    # the same-day no-op path.
    bumped = get_command_streak_entry(str(uid)).get("date") != today_ct
    streak = await update_command_streak(uid, today_ct)
    if bumped and guild_id is not None:
        if await persistence.try_set_record(guild_id, "command_streak", streak, uid, display_name):
            await announce_record(channel, "command_streak", display_name, streak, holder_id=uid)
    return streak
