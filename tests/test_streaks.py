"""Daily command-usage streak (src/streaks.py) + the !streaks admin command."""

import pytest

import src.state as _state
import src.persistence as _persistence
from src.streaks import effective_streak, update_command_streak

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio


# ── update_command_streak ─────────────────────────────────────────────────────

async def test_first_command_starts_streak_at_one():
    assert await update_command_streak(1, "2026-07-20") == 1
    assert _state.command_streak["1"] == {"date": "2026-07-20", "count": 1}


async def test_same_day_is_idempotent():
    await update_command_streak(1, "2026-07-20")
    assert await update_command_streak(1, "2026-07-20") == 1
    assert _state.command_streak["1"]["count"] == 1


async def test_consecutive_day_increments():
    await update_command_streak(1, "2026-07-20")
    assert await update_command_streak(1, "2026-07-21") == 2
    assert _state.command_streak["1"] == {"date": "2026-07-21", "count": 2}


async def test_gap_resets_to_one():
    _state.command_streak["1"] = {"date": "2026-07-18", "count": 9}
    assert await update_command_streak(1, "2026-07-20") == 1


async def test_month_boundary_counts_as_consecutive():
    _state.command_streak["1"] = {"date": "2026-06-30", "count": 4}
    assert await update_command_streak(1, "2026-07-01") == 5


# ── effective_streak ──────────────────────────────────────────────────────────

async def test_effective_streak_today_and_yesterday_alive():
    assert effective_streak({"date": "2026-07-20", "count": 3}, "2026-07-20") == 3
    assert effective_streak({"date": "2026-07-19", "count": 3}, "2026-07-20") == 3


async def test_effective_streak_stale_or_missing_is_zero():
    assert effective_streak({"date": "2026-07-17", "count": 3}, "2026-07-20") == 0
    assert effective_streak({"date": None, "count": 0}, "2026-07-20") == 0
    assert effective_streak({}, "2026-07-20") == 0


# ── persistence round-trip ────────────────────────────────────────────────────

async def test_command_streak_roundtrip(db):
    _state.command_streak.clear()
    _state.command_streak["1"] = {"date": "2026-07-20", "count": 3}
    await _persistence.save_command_streak(1)
    # Upsert path: bump the same row and save again.
    _state.command_streak["1"] = {"date": "2026-07-21", "count": 4}
    await _persistence.save_command_streak(1)
    _state.command_streak["2"] = {"date": "2026-07-19", "count": 1}
    await _persistence.save_command_streak(2)

    _state.command_streak.clear()
    await _persistence.init_db_state()
    assert _state.command_streak["1"] == {"date": "2026-07-21", "count": 4}
    assert _state.command_streak["2"] == {"date": "2026-07-19", "count": 1}


# ── !streaks command ──────────────────────────────────────────────────────────

async def _run_streaks(ctx):
    from src.cogs.utility_cog import UtilityCog
    cog = UtilityCog.__new__(UtilityCog)  # skip __init__; command doesn't use self.bot
    await UtilityCog.cmd_streaks.callback(cog, ctx)


async def test_streaks_lists_only_active_guild_members(monkeypatch):
    monkeypatch.setattr("src.cogs.utility_cog._ct_today", lambda: "2026-07-20")
    guild = FakeGuild(gid=1)
    alice = FakeMember(uid=1, display_name="Alice")
    bob = FakeMember(uid=2, display_name="Bob")
    guild.members = [alice, bob]

    _state.command_streak.update({
        "1": {"date": "2026-07-20", "count": 5},   # active (today)
        "2": {"date": "2026-07-19", "count": 2},   # active (yesterday)
        "3": {"date": "2026-07-20", "count": 9},   # not in this guild
        "4": {"date": "2026-07-10", "count": 30},  # broken streak
    })
    _state.gambler_streak.clear()
    _state.gambler_streak["2"] = {"date": "2026-07-20", "count": 7}

    ctx = FakeCtx(author=alice, guild=guild, command_name="streaks")
    await _run_streaks(ctx)

    assert len(ctx.sent_embeds) == 1
    embed = ctx.sent_embeds[0]
    cmd_field, gamble_field = embed.fields[0].value, embed.fields[1].value
    assert "Alice" in cmd_field and "5 days" in cmd_field
    assert "Bob" in cmd_field and "2 days" in cmd_field
    # Sorted descending: Alice (5) before Bob (2).
    assert cmd_field.index("Alice") < cmd_field.index("Bob")
    # Non-member uid=3 and broken-streak uid=4 excluded.
    assert "9 days" not in cmd_field and "30 days" not in cmd_field
    assert "Bob" in gamble_field and "7 days" in gamble_field


async def test_streaks_empty_shows_none(monkeypatch):
    monkeypatch.setattr("src.cogs.utility_cog._ct_today", lambda: "2026-07-20")
    _state.gambler_streak.clear()
    ctx = FakeCtx(guild=FakeGuild(gid=1), command_name="streaks")
    await _run_streaks(ctx)
    embed = ctx.sent_embeds[0]
    assert embed.fields[0].value == "*None*"
    assert embed.fields[1].value == "*None*"


async def test_streaks_rejects_dm():
    ctx = FakeCtx(command_name="streaks")
    ctx.guild = None
    await _run_streaks(ctx)
    assert "server" in ctx.sent_embeds[0].description
