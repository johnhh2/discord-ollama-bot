"""Sequential-day command-usage streak (any command, gambling included).

Bumped from EventsCog.on_command_completion the first time a user runs any
command in a CT day (5am rollover, same day definition as `_ct_today`).
Distinct from the gambler streak in src/gambling/scratchoff.py, which only
counts full scratchoff days.
"""
import datetime

# Module (not name) import so the conftest save-fn stubs apply at call time.
from src import persistence, state


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
    """Bump the user's daily command-usage streak. Returns the new count.

    Idempotent within a day: the first command of the day extends (or resets)
    the streak and persists; every later command is a no-op read.
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
