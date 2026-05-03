"""Tier G: small helpers + auto-daily reward guard.

- resolve_role: parses <@&ID> mentions and falls back to name lookup.
- format_uptime: time arithmetic against state.bot_start_time.
- _auto_daily: once-per-day guard; awards DAILY_REWARD on first eligible
  interaction, then becomes a no-op for the rest of today.
"""
import time
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.economy as _economy
from src.helpers import resolve_role, format_uptime
from src.events import _auto_daily
from src.config import DAILY_REWARD

from tests.fakes.discord import FakeMember, FakeGuild, FakeRole


# ── resolve_role ──────────────────────────────────────────────────────────────

class TestResolveRole:
    def test_mention_format_returns_role_by_id(self):
        role = FakeRole(role_id=12345, name="VIP")
        guild = FakeGuild()
        guild.roles = [role]
        assert resolve_role(guild, "<@&12345>") is role

    def test_mention_with_unknown_id_returns_none(self):
        guild = FakeGuild()
        guild.roles = [FakeRole(role_id=1, name="X")]
        assert resolve_role(guild, "<@&99999>") is None

    def test_malformed_mention_returns_none(self):
        guild = FakeGuild()
        guild.roles = [FakeRole(role_id=1, name="<@&abc>")]
        # The literal string "<@&abc>" matches the mention prefix but the
        # int() conversion fails → None.
        assert resolve_role(guild, "<@&abc>") is None

    def test_plain_name_resolves(self):
        guild = FakeGuild()
        target = FakeRole(role_id=7, name="Admins")
        guild.roles = [FakeRole(role_id=8, name="Other"), target]
        assert resolve_role(guild, "Admins") is target

    def test_plain_name_unknown_returns_none(self):
        guild = FakeGuild()
        guild.roles = [FakeRole(role_id=1, name="A")]
        assert resolve_role(guild, "Nonexistent") is None

    def test_strips_whitespace(self):
        guild = FakeGuild()
        target = FakeRole(role_id=7, name="VIP")
        guild.roles = [target]
        assert resolve_role(guild, "  <@&7>  ") is target


# ── format_uptime ─────────────────────────────────────────────────────────────

class TestFormatUptime:
    def test_short_uptime(self, monkeypatch):
        # 2 minutes elapsed.
        monkeypatch.setattr("src.helpers.time.monotonic", lambda: 100.0)
        monkeypatch.setattr(_state, "bot_start_time", 100.0 - 120)
        assert format_uptime() == "0d 0h 2m"

    def test_hours_and_minutes(self, monkeypatch):
        # 3h 45m elapsed.
        monkeypatch.setattr("src.helpers.time.monotonic", lambda: 100.0)
        monkeypatch.setattr(_state, "bot_start_time", 100.0 - (3 * 3600 + 45 * 60))
        assert format_uptime() == "0d 3h 45m"

    def test_days_hours_minutes(self, monkeypatch):
        # 2d 5h 17m elapsed.
        monkeypatch.setattr("src.helpers.time.monotonic", lambda: 100.0)
        elapsed = 2 * 86400 + 5 * 3600 + 17 * 60
        monkeypatch.setattr(_state, "bot_start_time", 100.0 - elapsed)
        assert format_uptime() == "2d 5h 17m"

    def test_zero_uptime(self, monkeypatch):
        """Just-started bot: all zeros."""
        monkeypatch.setattr("src.helpers.time.monotonic", lambda: 100.0)
        monkeypatch.setattr(_state, "bot_start_time", 100.0)
        assert format_uptime() == "0d 0h 0m"

    def test_seconds_truncate_to_minute(self, monkeypatch):
        """59s elapsed should still render as 0m."""
        monkeypatch.setattr("src.helpers.time.monotonic", lambda: 100.0)
        monkeypatch.setattr(_state, "bot_start_time", 100.0 - 59)
        assert format_uptime() == "0d 0h 0m"


# ── _auto_daily guard ─────────────────────────────────────────────────────────

class _Channel:
    def __init__(self):
        self.send = AsyncMock()


class _Msg:
    def __init__(self, author, channel):
        self.author = author
        self.channel = channel


@pytest.mark.asyncio
async def test_auto_daily_first_call_grants_daily_reward(db):
    user = FakeMember(uid=5001)
    channel = _Channel()
    msg = _Msg(user, channel)

    await _auto_daily(msg)

    # Balance bumped by DAILY_REWARD; daily_date set to today.
    assert await _economy.get_balance(user.id) == DAILY_REWARD
    assert _state.economy["users"][str(user.id)]["daily_date"] == _economy._ct_today()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_daily_second_call_same_day_is_noop(db):
    user = FakeMember(uid=5002)
    channel = _Channel()
    msg = _Msg(user, channel)

    await _auto_daily(msg)
    bal_after_first = await _economy.get_balance(user.id)
    assert bal_after_first == DAILY_REWARD

    # Second call same day: no additional grant.
    await _auto_daily(msg)
    assert await _economy.get_balance(user.id) == bal_after_first
    # Only the first call sent a message.
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_auto_daily_after_day_rollover_grants_again(db):
    user = FakeMember(uid=5003)
    channel = _Channel()
    msg = _Msg(user, channel)

    # Pretend the user claimed yesterday.
    await _economy._ensure_user(user.id)
    _state.economy["users"][str(user.id)]["daily_date"] = "1999-01-01"

    await _auto_daily(msg)
    assert await _economy.get_balance(user.id) == DAILY_REWARD
    assert _state.economy["users"][str(user.id)]["daily_date"] == _economy._ct_today()


@pytest.mark.asyncio
async def test_auto_daily_first_ever_claim_sets_last_daily_timestamp(db):
    """For brand-new users (last_daily == 0), _auto_daily records the
    current time. Returning users keep their existing last_daily."""
    new_user = FakeMember(uid=5004)
    channel = _Channel()

    before = time.time()
    await _auto_daily(_Msg(new_user, channel))
    after = time.time()

    rec = _state.economy["users"][str(new_user.id)]
    assert before <= rec["last_daily"] <= after


@pytest.mark.asyncio
async def test_auto_daily_returning_user_does_not_overwrite_last_daily(db):
    """Returning users keep their existing last_daily value (only the
    first-ever claim sets it)."""
    user = FakeMember(uid=5005)
    channel = _Channel()

    await _economy._ensure_user(user.id)
    # Returning user: stale daily_date, but last_daily is already set.
    _state.economy["users"][str(user.id)]["daily_date"] = "1999-01-01"
    _state.economy["users"][str(user.id)]["last_daily"] = 123.0

    await _auto_daily(_Msg(user, channel))

    # last_daily NOT overwritten (the `if is_new` branch was skipped).
    assert _state.economy["users"][str(user.id)]["last_daily"] == 123.0
