"""Coin events (!event): the weekly budget a server admin's drops pay from.

A bot admin's event pays every reactor without limit. A server admin's event
pays out of the guild's weekly budget (EVENT_WEEKLY_BUDGET), kept in the
guild_settings blob as cfg["event_budget"] = {"week": <Monday's date>,
"spent": n} — no table, like the dailies keep list. The week runs Monday to
Monday on the 5am CT gameplay-day boundary. Every 🪙 reaction claims the
per-reaction amount synchronously (see CLAUDE.md: Concurrency), and the event
ends as soon as the budget can't cover another reaction, so one reaction can
never overdraw it.
"""
import datetime
from zoneinfo import ZoneInfo

from src.config import EVENT_WEEKLY_BUDGET, DAILY_RESET_HOUR
from src.economy import _ct_today_date
from src.guild_config import get_guild_cfg


def event_week_key(today: "datetime.date | None" = None) -> str:
    """The Monday that starts the gameplay week `today` falls in."""
    today = today or _ct_today_date()
    return (today - datetime.timedelta(days=today.weekday())).isoformat()


def next_event_week_reset_ts() -> int:
    """Unix timestamp of the next budget reset: 5am CT next Monday."""
    monday = datetime.date.fromisoformat(event_week_key()) + datetime.timedelta(days=7)
    reset = datetime.datetime.combine(
        monday, datetime.time(DAILY_RESET_HOUR, 0), tzinfo=ZoneInfo("America/Chicago"),
    )
    return int(reset.timestamp())


def _budget(guild_id: int) -> dict:
    # A row from an earlier week is stale: start this week at 0.
    cfg = get_guild_cfg(guild_id)
    row = cfg.get("event_budget")
    week = event_week_key()
    if not isinstance(row, dict) or row.get("week") != week:
        row = cfg["event_budget"] = {"week": week, "spent": 0}
    return row


def event_budget_remaining(guild_id: int) -> int:
    return max(0, EVENT_WEEKLY_BUDGET - int(_budget(guild_id)["spent"]))


def claim_event_budget(guild_id: int, amount: int) -> bool:
    """Reserve `amount` of this week's budget, or refuse if it doesn't fit.
    Synchronous on purpose; the caller saves guild settings afterwards."""
    row = _budget(guild_id)
    if row["spent"] + amount > EVENT_WEEKLY_BUDGET:
        return False
    row["spent"] += amount
    return True


def refund_event_budget(guild_id: int, amount: int) -> None:
    """Undo a claim whose payment failed."""
    row = _budget(guild_id)
    row["spent"] = max(0, row["spent"] - amount)


def event_budget_status(guild_id: int) -> str:
    """One sentence for the usage and refusal embeds."""
    return (
        f"This server's coin events can pay out **{event_budget_remaining(guild_id):,} 🪙** more "
        f"this week (of {EVENT_WEEKLY_BUDGET:,}; resets <t:{next_event_week_reset_ts()}:R>). "
        "Bot admins' events are unlimited."
    )
