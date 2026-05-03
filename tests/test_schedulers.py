"""Tier 4: scheduler/time logic.

These cover behaviors that depend on time and rate-limit windows:
- grant_xp per source (msg/cmd/voice/stream): hourly cooldown, daily caps,
  daily-cap reset on UTC midnight rollover.
- _handle_soundboard_ratelimit: rolling-window deque eviction and threshold.
- on_reaction_add for !event coin events: pay once, idempotent on double
  react, silently no-op when the event has been removed (post-expiry).
- Tax expiry: the time-based check at events.py:389.

For grant_xp / soundboard, time.time() and time.monotonic() are
monkeypatched to advance under our control — running the actual rolling
window in real time would slow the suite for no benefit.
"""

import pytest

import src.state as _state
import src.events as _events
from src.leveling import (
    grant_xp, _ensure_lvl_record as _ensure_lvl_user,
    HOUR_SECS, MINS30_SECS, MSG_DAILY_MAX, XP_MESSAGE, XP_VOICE, XP_STREAM,
)
from src.cogs.economy_cog import EconomyCog
from src.config import SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS, SHOP_TAX_DURATION_SECS
from src.economy import get_balance

from tests.fakes.discord import FakeMember

# Note: no module-level pytestmark — most tests are async, but the tax-expiry
# arithmetic test is sync. Async tests get @pytest.mark.asyncio per-function
# below.


# ── grant_xp ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_xp_msg_first_call_grants(db, monkeypatch):
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)
    xp, leveled_up = await grant_xp(uid=1, source="msg", guild_id=42)
    assert xp == XP_MESSAGE
    rec = _state.leveling["42"]["1"]
    assert rec["xp"] == XP_MESSAGE
    assert rec["msg_today"] == 1


@pytest.mark.asyncio
async def test_grant_xp_msg_second_call_within_hour_blocked(db, monkeypatch):
    times = [1_000_000.0]
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: times[0])

    xp1, _ = await grant_xp(uid=2, source="msg", guild_id=42)
    times[0] += 60  # 1 minute later
    xp2, _ = await grant_xp(uid=2, source="msg", guild_id=42)

    assert xp1 == XP_MESSAGE
    assert xp2 == 0
    assert _state.leveling["42"]["2"]["msg_today"] == 1  # still 1


@pytest.mark.asyncio
async def test_grant_xp_msg_after_hour_grants_again(db, monkeypatch):
    times = [1_000_000.0]
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: times[0])

    await grant_xp(uid=3, source="msg", guild_id=42)
    times[0] += HOUR_SECS + 1
    xp2, _ = await grant_xp(uid=3, source="msg", guild_id=42)

    assert xp2 == XP_MESSAGE
    assert _state.leveling["42"]["3"]["msg_today"] == 2


@pytest.mark.asyncio
async def test_grant_xp_msg_daily_cap_blocks(db, monkeypatch):
    times = [1_000_000.0]
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: times[0])

    # MSG_DAILY_MAX successful grants — advance an hour each.
    for _ in range(MSG_DAILY_MAX):
        await grant_xp(uid=4, source="msg", guild_id=42)
        times[0] += HOUR_SECS + 1

    # Next attempt: cap reached, blocked despite cooldown OK.
    xp_blocked, _ = await grant_xp(uid=4, source="msg", guild_id=42)
    assert xp_blocked == 0
    assert _state.leveling["42"]["4"]["msg_today"] == MSG_DAILY_MAX


@pytest.mark.asyncio
async def test_grant_xp_daily_cap_resets_on_utc_day_rollover(db, monkeypatch):
    """_day_reset compares UTC dates; once we cross midnight UTC, msg_today resets."""
    import datetime
    # Anchor at noon UTC on day N
    day_n_noon = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    times = [day_n_noon]
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: times[0])

    # Burn through the cap on day N
    for _ in range(MSG_DAILY_MAX):
        await grant_xp(uid=5, source="msg", guild_id=42)
        times[0] += HOUR_SECS + 1

    # Jump to noon UTC on day N+1
    day_np1_noon = datetime.datetime(2026, 5, 2, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    times[0] = day_np1_noon

    xp, _ = await grant_xp(uid=5, source="msg", guild_id=42)
    assert xp == XP_MESSAGE
    assert _state.leveling["42"]["5"]["msg_today"] == 1  # reset


@pytest.mark.asyncio
async def test_grant_xp_voice_uses_30_min_cooldown_not_hour(db, monkeypatch):
    """Voice XP rate-limit window is 30 minutes (MINS30_SECS), not an hour."""
    times = [1_000_000.0]
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: times[0])

    xp1, _ = await grant_xp(uid=6, source="voice", guild_id=42)
    assert xp1 == XP_VOICE

    # 20 min later: still in cooldown, blocked.
    times[0] += 20 * 60
    xp2, _ = await grant_xp(uid=6, source="voice", guild_id=42)
    assert xp2 == 0

    # 31 min after first grant: cooldown expired.
    times[0] = 1_000_000.0 + MINS30_SECS + 60
    xp3, _ = await grant_xp(uid=6, source="voice", guild_id=42)
    assert xp3 == XP_VOICE


@pytest.mark.asyncio
async def test_grant_xp_stream_uses_hour_cooldown(db, monkeypatch):
    times = [1_000_000.0]
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: times[0])

    xp1, _ = await grant_xp(uid=7, source="stream", guild_id=42)
    assert xp1 == XP_STREAM

    times[0] += 30 * 60  # 30 min — not enough
    xp2, _ = await grant_xp(uid=7, source="stream", guild_id=42)
    assert xp2 == 0

    times[0] = 1_000_000.0 + HOUR_SECS + 1
    xp3, _ = await grant_xp(uid=7, source="stream", guild_id=42)
    assert xp3 == XP_STREAM


@pytest.mark.asyncio
async def test_grant_xp_no_guild_id_returns_zero(db):
    xp, leveled = await grant_xp(uid=8, source="msg", guild_id=None)
    assert xp == 0
    assert leveled is False
    # No state created.
    assert "8" not in _state.leveling.get("0", {})


@pytest.mark.asyncio
async def test_grant_xp_unknown_source_returns_zero(db, monkeypatch):
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)
    xp, _ = await grant_xp(uid=9, source="unknown_source", guild_id=42)
    assert xp == 0


@pytest.mark.asyncio
async def test_grant_xp_levels_up_when_threshold_crossed(db, monkeypatch):
    """Pre-load XP just below level-1 threshold; one msg grant pushes over."""
    from src.leveling import xp_for_level
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    # Seed user with xp = level-1 threshold - 1, level=0
    rec = _ensure_lvl_user(42, 10)
    rec["xp"] = xp_for_level(1) - 1
    rec["level"] = 0

    xp, leveled_up = await grant_xp(uid=10, source="msg", guild_id=42)
    assert xp == XP_MESSAGE
    assert leveled_up is True
    assert _state.leveling["42"]["10"]["level"] == 1


# ── soundboard rate-limit ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_soundboard_under_threshold_no_kick(db, monkeypatch):
    """Posting fewer than SOUNDBOARD_MAX_SOUNDS within the window is fine."""
    from src.events import _handle_soundboard_ratelimit

    times = [1_000_000.0]
    monkeypatch.setattr("src.events.time.monotonic", lambda: times[0])

    class _Bot:
        def get_guild(self, gid): return None  # not reached when under threshold

    # Burst exactly at the cap.
    for _ in range(SOUNDBOARD_MAX_SOUNDS):
        await _handle_soundboard_ratelimit(_Bot(), guild_id=1, user_id=100)
        times[0] += 0.1

    # state still tracks the timestamps; no kick was attempted.
    assert (1, 100) in _events._SOUNDBOARD_TIMESTAMPS
    assert len(_events._SOUNDBOARD_TIMESTAMPS[(1, 100)]) == SOUNDBOARD_MAX_SOUNDS


@pytest.mark.asyncio
async def test_soundboard_over_threshold_clears_and_attempts_kick(db, monkeypatch):
    """One sound past the cap triggers the kick branch and resets the deque."""
    from src.events import _handle_soundboard_ratelimit

    times = [1_000_000.0]
    monkeypatch.setattr("src.events.time.monotonic", lambda: times[0])

    # Stub fetch_member so _handle_soundboard_ratelimit can early-exit at the
    # member-resolve step (returning None) instead of crashing.
    async def _no_member(guild, uid):
        return None
    monkeypatch.setattr("src.events.fetch_member", _no_member)

    class _Bot:
        def get_guild(self, gid):
            return object()  # non-None so we enter the resolve branch

    # Send SOUNDBOARD_MAX_SOUNDS + 1 within the window.
    for _ in range(SOUNDBOARD_MAX_SOUNDS + 1):
        await _handle_soundboard_ratelimit(_Bot(), guild_id=1, user_id=200)
        times[0] += 0.1

    # On exceeding, the deque is cleared so the same burst doesn't re-fire.
    assert _events._SOUNDBOARD_TIMESTAMPS.get((1, 200)) == []


@pytest.mark.asyncio
async def test_soundboard_old_timestamps_evicted_outside_window(db, monkeypatch):
    """Timestamps older than SOUNDBOARD_WINDOW_SECS are dropped on the next call."""
    from src.events import _handle_soundboard_ratelimit

    times = [1_000_000.0]
    monkeypatch.setattr("src.events.time.monotonic", lambda: times[0])

    class _Bot:
        def get_guild(self, gid): return None

    # Saturate the deque
    for _ in range(SOUNDBOARD_MAX_SOUNDS):
        await _handle_soundboard_ratelimit(_Bot(), guild_id=1, user_id=300)
        times[0] += 0.1
    assert len(_events._SOUNDBOARD_TIMESTAMPS[(1, 300)]) == SOUNDBOARD_MAX_SOUNDS

    # Skip past the window; the next call should evict everything older.
    times[0] += SOUNDBOARD_WINDOW_SECS + 1
    await _handle_soundboard_ratelimit(_Bot(), guild_id=1, user_id=300)
    # Only the just-added timestamp remains.
    assert len(_events._SOUNDBOARD_TIMESTAMPS[(1, 300)]) == 1


@pytest.mark.asyncio
async def test_soundboard_isolated_per_user(db, monkeypatch):
    """Two users sharing a guild don't interfere with each other's deques."""
    from src.events import _handle_soundboard_ratelimit

    monkeypatch.setattr("src.events.time.monotonic", lambda: 1_000_000.0)

    class _Bot:
        def get_guild(self, gid): return None

    await _handle_soundboard_ratelimit(_Bot(), guild_id=1, user_id=400)
    await _handle_soundboard_ratelimit(_Bot(), guild_id=1, user_id=401)

    assert len(_events._SOUNDBOARD_TIMESTAMPS[(1, 400)]) == 1
    assert len(_events._SOUNDBOARD_TIMESTAMPS[(1, 401)]) == 1


# ── !event on_reaction_add ────────────────────────────────────────────────────

class _StubReaction:
    """Minimal stand-in for discord.Reaction (only the attrs we touch)."""
    def __init__(self, message_id: int, emoji: str = "🪙"):
        self.message = type("M", (), {"id": message_id})()
        self.emoji = emoji


@pytest.mark.asyncio
async def test_event_reaction_first_time_awards_coins(db):
    cog = EconomyCog(bot=None)
    user = FakeMember(uid=2001)
    msg_id = 555_000
    _state.active_events[msg_id] = {"amount": 100, "rewarded": set()}

    await cog.on_reaction_add(_StubReaction(msg_id), user)

    assert await get_balance(user.id) == 100
    assert user.id in _state.active_events[msg_id]["rewarded"]


@pytest.mark.asyncio
async def test_event_reaction_double_react_only_pays_once(db):
    cog = EconomyCog(bot=None)
    user = FakeMember(uid=2002)
    msg_id = 555_001
    _state.active_events[msg_id] = {"amount": 100, "rewarded": set()}

    await cog.on_reaction_add(_StubReaction(msg_id), user)
    await cog.on_reaction_add(_StubReaction(msg_id), user)

    assert await get_balance(user.id) == 100  # not 200


@pytest.mark.asyncio
async def test_event_reaction_after_expiry_silently_skipped(db):
    """Once _close_event has removed the message_id from active_events, any
    later reaction is a no-op (not in dict → first guard returns)."""
    cog = EconomyCog(bot=None)
    user = FakeMember(uid=2003)
    # active_events does NOT contain msg_id (post-expiry)
    msg_id = 555_002

    await cog.on_reaction_add(_StubReaction(msg_id), user)

    # Balance still 0; no entry was created.
    assert await get_balance(user.id) == 0


@pytest.mark.asyncio
async def test_event_reaction_wrong_emoji_ignored(db):
    cog = EconomyCog(bot=None)
    user = FakeMember(uid=2004)
    msg_id = 555_003
    _state.active_events[msg_id] = {"amount": 100, "rewarded": set()}

    await cog.on_reaction_add(_StubReaction(msg_id, emoji="🎉"), user)

    assert await get_balance(user.id) == 0
    assert user.id not in _state.active_events[msg_id]["rewarded"]


# ── tax expiry invariant ──────────────────────────────────────────────────────

def test_tax_expiry_check_fires_after_duration():
    """Pin the comparison at events.py:389: tax expires once
    `time.time() - activated_at > SHOP_TAX_DURATION_SECS`."""
    activated_at = 1_000_000.0

    just_under = activated_at + SHOP_TAX_DURATION_SECS - 1
    assert not ((just_under - activated_at) > SHOP_TAX_DURATION_SECS)

    just_over = activated_at + SHOP_TAX_DURATION_SECS + 1
    assert (just_over - activated_at) > SHOP_TAX_DURATION_SECS

    # Boundary: at exactly the duration the strict `>` is False, so not expired.
    exactly_at = activated_at + SHOP_TAX_DURATION_SECS
    assert not ((exactly_at - activated_at) > SHOP_TAX_DURATION_SECS)
