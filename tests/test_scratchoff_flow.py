"""Tier E: scratchoff daily-limit logic.

The eligibility check at the top of cmd_scratchoff was extracted into
scratchoff_attempts_remaining(user, today) so the rollover + cap can be
tested in isolation. The full command (RNG card draw, payout tiers,
streak/role logic) is integration territory; the daily-limit invariant
is what matters here.
"""

import asyncio

import pytest

import src.state as _state
import src.economy as _economy
from src.gambling.scratchoff import (
    ScratchoffCog, scratchoff_attempts_remaining,
)

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeChannel


class TestScratchoffAttemptsRemaining:
    def test_fresh_user_gets_three(self):
        user = {}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        # Side effect: scratch_date set to today, scratch_used initialized.
        assert user["scratch_date"] == "2026-05-02"
        assert user["scratch_used"] == 0

    def test_partial_today(self):
        user = {"scratch_date": "2026-05-02", "scratch_used": 1}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 2
        # Same-day call doesn't reset.
        assert user["scratch_used"] == 1

    def test_exhausted_today(self):
        user = {"scratch_date": "2026-05-02", "scratch_used": 3}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 0

    def test_stale_date_resets_to_full(self):
        """User had used all 3 yesterday — today they're back to 3."""
        user = {"scratch_date": "2026-05-01", "scratch_used": 3}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        assert user["scratch_date"] == "2026-05-02"
        assert user["scratch_used"] == 0

    def test_stale_partial_also_resets(self):
        user = {"scratch_date": "2026-05-01", "scratch_used": 1}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        assert user["scratch_used"] == 0

    def test_missing_scratch_used_treated_as_zero(self):
        """A user record that has scratch_date but lacks scratch_used (stored
        from an older schema) shouldn't crash."""
        user = {"scratch_date": "2026-05-02"}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        # Side effect normalizes the field.
        assert user["scratch_used"] == 0

    def test_overflow_scratch_used_clamped_at_zero(self):
        """Defensive: a corrupted scratch_used > 3 must not produce a
        negative remaining count."""
        user = {"scratch_date": "2026-05-02", "scratch_used": 99}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 0

    def test_does_not_mutate_when_date_matches(self):
        """When scratch_date already == today, no reset is performed."""
        user = {"scratch_date": "2026-05-02", "scratch_used": 2}
        scratchoff_attempts_remaining(user, "2026-05-02")
        assert user == {"scratch_date": "2026-05-02", "scratch_used": 2}


class _StubBot:
    def __init__(self):
        self.user = type("U", (), {"id": 999_999_999})()
        self.cogs = {}


@pytest.mark.asyncio
async def test_scratches_role_grant_announced_after_third_card(db, monkeypatch):
    """!scratches plays 3 scratchoffs in one invocation. When the user crosses
    the 3-day full-scratch streak on the third card, the Gamblers role-grant
    announcement must arrive AFTER the third card embed — not between cards
    2 and 3, where it used to land."""
    today = "2026-05-02"
    yesterday = "2026-05-01"

    # Pin "today" so the test is deterministic regardless of clock/DST.
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: today)

    # Enable the Gamblers role for this guild.
    guild_id = 42
    _state.guild_settings[str(guild_id)] = {"gambler_role_enabled": True}

    # Seed a 2-day streak ending yesterday — the third card today bumps it
    # to 3 and trips the role grant.
    _state.gambler_streak[str(1)] = {"date": yesterday, "count": 2}

    # Fund + reset the user so all 3 attempts are available.
    await _economy._ensure_user(1)
    _state.economy["users"]["1"]["scratch_date"] = today
    _state.economy["users"]["1"]["scratch_used"] = 0

    # Build a ctx whose ctx.send and ctx.channel.send share a single ordered
    # event log. The role announcement uses `channel.send`; the card embeds
    # use `ctx.send`. Interleaving them in one list is what proves ordering.
    author = FakeMember(uid=1, display_name="player")
    guild = FakeGuild(gid=guild_id)
    channel = FakeChannel(ch_id=100)
    ctx = FakeCtx(author=author, guild=guild, channel=channel)
    ctx.bot = _StubBot()

    events: list[tuple[str, str]] = []

    async def record_ctx_send(content=None, *, embed=None, **kwargs):
        # Card embeds post via ctx.send(embed=...).
        events.append(("card", embed.title if embed is not None else str(content)))
        return None

    async def record_channel_send(content=None, *, embed=None, **kwargs):
        events.append(("role_announce", content or ""))
        return None

    ctx.send = record_ctx_send
    ctx.channel.send = record_channel_send

    # Stub out the role-acquisition machinery: real toggle_member_role would
    # call discord.Member.add_roles, which our FakeMember doesn't implement.
    # We only care that the announcement send() lands at the right point.
    class _StubRole:
        pass

    async def _fake_get_role(g):
        return _StubRole()

    async def _fake_toggle(member, role, add, reason=""):
        return True

    monkeypatch.setattr(
        "src.gambling.scratchoff.get_or_create_gamblers_role", _fake_get_role
    )
    monkeypatch.setattr(
        "src.gambling.scratchoff.toggle_member_role", _fake_toggle
    )

    cog = ScratchoffCog(bot=_StubBot())
    # !scratches is a thin wrapper that invokes cmd_scratchoff with count=3.
    # Calling the underlying callback directly avoids needing a real
    # discord.py Context.invoke().
    await cog.cmd_scratchoff.callback(cog, ctx, count=3)

    # Three card embeds, one role announcement, all in this list.
    card_indices = [i for i, (kind, _) in enumerate(events) if kind == "card"]
    role_indices = [i for i, (kind, _) in enumerate(events) if kind == "role_announce"]

    assert len(card_indices) == 3, f"expected 3 cards, got events={events}"
    assert len(role_indices) == 1, f"expected 1 role announcement, got events={events}"

    # The fix: announcement must come after the third (final) card, not between
    # the 2nd and 3rd cards as the previous version did.
    assert role_indices[0] > card_indices[2], (
        f"role announcement landed mid-sequence: events={events}"
    )


@pytest.mark.asyncio
async def test_concurrent_scratchoff_invocations_cap_at_three(monkeypatch):
    """Spamming !scratchoff in rapid succession must not exceed the daily cap.

    Previously, the command checked `scratch_used` at the top, then awaited
    Discord I/O before incrementing the counter inside the per-card loop.
    Three concurrent invocations could each pass the gate, run their full
    loops, and the user would end up with 9 cards instead of 3. The fix
    reserves the attempt counter synchronously before any await yields the
    event loop.

    Forces real event-loop yielding inside the loop body via patched
    add_balance — without that, the conftest noop stubs return synchronously
    and never expose the interleaving the bug needs.
    """
    today = "2026-05-02"
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: today)

    await _economy._ensure_user(1)
    _state.economy["users"]["1"]["scratch_date"] = today
    _state.economy["users"]["1"]["scratch_used"] = 0
    _state.economy["users"]["1"]["balance"] = 0

    # Force the per-card await to yield to the event loop so concurrent
    # gather() callers can interleave at exactly the spot where the unfixed
    # code raced. add_balance is the first await inside the per-card loop.
    async def _yielding_add_balance(*args, **kwargs):
        await asyncio.sleep(0)

    async def _noop_async(*args, **kwargs):
        return None

    async def _noop_grant_xp(*args, **kwargs):
        return (None, False)

    monkeypatch.setattr("src.gambling.scratchoff.add_balance", _yielding_add_balance)
    # The scratchoff module imports save_economy / grant_xp by name, so the
    # global conftest stubs don't reach these bindings. Patch them too so
    # the test doesn't drift into real DB I/O via save_leveling.
    monkeypatch.setattr("src.gambling.scratchoff.save_economy", _noop_async)
    monkeypatch.setattr("src.gambling.scratchoff.grant_xp", _noop_grant_xp)

    author = FakeMember(uid=1, display_name="player")
    ctx = FakeCtx(author=author, channel=FakeChannel(ch_id=100))
    # FakeCtx defaults to a real FakeGuild when guild=None is passed, which
    # routes through the streak/role path; null it explicitly to keep the
    # test focused on the cap.
    ctx.guild = None
    ctx.bot = _StubBot()

    cards_drawn: list = []

    async def record_send(content=None, *, embed=None, **kwargs):
        if embed is not None and embed.title == "🎫 Scratchoff":
            cards_drawn.append(embed)
        return None

    ctx.send = record_send

    cog = ScratchoffCog(bot=_StubBot())

    # Three concurrent invocations of `!scratchoff 3` (the alias `!scratches`
    # uses count=3). asyncio.gather() lets all three start before any of them
    # complete, exposing the race.
    await asyncio.gather(
        cog.cmd_scratchoff.callback(cog, ctx, count=3),
        cog.cmd_scratchoff.callback(cog, ctx, count=3),
        cog.cmd_scratchoff.callback(cog, ctx, count=3),
    )

    # The cap is 3 cards per day total — not 3 per invocation.
    assert len(cards_drawn) == 3, (
        f"daily cap breached: drew {len(cards_drawn)} cards across 3 concurrent "
        f"invocations (expected 3)"
    )
    assert _state.economy["users"]["1"]["scratch_used"] == 3
