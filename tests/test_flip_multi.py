"""!flip <amount> [n] — multi-coin behavior.

Pins the parts that are easy to silently regress when refactoring:
- total cost is amount * n (charged once via shop_charge)
- each rigged-flip entry is consumed per-coin (single-use, not per command)
- net P/L and final balance reflect every coin
"""
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.economy as _economy
from src.gambling.flip import FlipCog, play_flip
from src.gambling.play_again import PlayAgainView

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


class _StubBot:
    def __init__(self):
        self.user = type("U", (), {"id": 999_999_999})()


def _ctx(uid: int = 1):
    author = FakeMember(uid=uid, display_name="player")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    return ctx


@pytest.mark.asyncio
async def test_flip_n_consumes_one_rigged_per_coin_and_settles_net(db, monkeypatch):
    """3 rigged flips + 2 fair losses on a 5-coin flip:
    - rigged_flips entry is consumed exactly 3 times (1 per coin, not 1 per command)
    - balance = start - 5*bet + 3 wins * 2*bet
    """
    cog = FlipCog(bot=_StubBot())
    ctx = _ctx(uid=77)
    start = 100_000
    await _economy.add_balance(77, start)

    _state.rigged_flips[77] = 3
    # Force the non-rigged coins to lose.
    monkeypatch.setattr(random, "random", lambda: 0.99)

    bet = 1_000
    n = 5
    await cog.cmd_flip.callback(cog, ctx, amount=str(bet), n=n)

    # 3 wins out of 5: net = 3*2*bet - 5*bet = bet
    expected_balance = start - n * bet + 3 * (2 * bet)
    assert await _economy.get_balance(77) == expected_balance
    # All 3 rigged uses were consumed and the entry was removed.
    assert 77 not in _state.rigged_flips

    # Gambling event recorded the signed net (gained=bet, since net > 0),
    # keyed (guild_id, uid_str) — the ctx guild is 42.
    rec = _state.gambling_today_by_user.get((42, "77"), {})
    assert rec.get("gained") == bet
    assert rec.get("lost", 0) == 0


# ── Flip Again / Double buttons ───────────────────────────────────────────────
#
# A hand-typed !flip result carries two buttons: "Flip Again" (same stake) and
# "Double" (2× the per-coin stake, same n and side). The dailies 🪙 claim goes
# through play_flip's default and gets none. The view's own contract is pinned
# in test_play_again_view.py.


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(),
        )


def _result_views(ctx) -> list:
    return [c.kwargs["view"] for c in ctx.channel.send.call_args_list
            if c.kwargs.get("view") is not None]


@pytest.mark.asyncio
async def test_flip_offers_again_and_double_buttons(db, monkeypatch):
    cog = FlipCog(bot=_StubBot())
    ctx = _ctx(uid=80)
    await _economy.add_balance(80, 100_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)   # tails → heads loses

    await cog.cmd_flip.callback(cog, ctx, amount="1000")

    views = _result_views(ctx)
    assert len(views) == 1
    view = views[0]
    assert isinstance(view, PlayAgainView)
    assert view.message is not None
    assert [b.label for b in view.children] == ["Flip Again · 1,000 🪙", "Double · 2,000 🪙"]
    assert [b.stake for b in view.children] == [1000, 2000]
    view.stop()


@pytest.mark.asyncio
async def test_flip_multi_coin_buttons_show_per_coin_stake(db, monkeypatch):
    cog = FlipCog(bot=_StubBot())
    ctx = _ctx(uid=81)
    await _economy.add_balance(81, 100_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await cog.cmd_flip.callback(cog, ctx, amount="1000", n=5)

    view = _result_views(ctx)[0]
    assert [b.label for b in view.children] == ["Flip Again · 5 × 1,000 🪙", "Double · 5 × 2,000 🪙"]
    assert [b.stake for b in view.children] == [1000, 2000]
    view.stop()


@pytest.mark.asyncio
async def test_flip_declined_bet_offers_no_buttons(db, monkeypatch):
    cog = FlipCog(bot=_StubBot())
    ctx = _ctx(uid=82)
    await _economy.add_balance(82, 500)

    await cog.cmd_flip.callback(cog, ctx, amount="1000")

    assert _result_views(ctx) == []


@pytest.mark.asyncio
async def test_play_flip_default_has_no_buttons(db, monkeypatch):
    """play_flip's default (the dailies 🪙 claim path) attaches nothing."""
    ctx = _ctx(uid=83)
    await _economy.add_balance(83, 100_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await play_flip(ctx.author, ctx.channel, ctx.guild, 1000)

    assert ctx.channel.send.await_count == 1
    assert _result_views(ctx) == []


@pytest.mark.asyncio
async def test_flip_double_click_flips_double_same_n_and_side(db, monkeypatch):
    """Double on a `!flip 1000 2 tails` result charges 2 × 2,000, keeps n and
    side, and offers a fresh pair scaled to the new stake."""
    cog = FlipCog(bot=_StubBot())
    ctx = _ctx(uid=84)
    await _economy.add_balance(84, 100_000)
    monkeypatch.setattr(random, "random", lambda: 0.01)   # heads → tails loses

    await cog.cmd_flip.callback(cog, ctx, amount="1000", n=2, side="tails")
    first = _result_views(ctx)[0]
    bal_after_first = await _economy.get_balance(84)
    assert bal_after_first == 100_000 - 2 * 1000

    interaction = _FakeInteraction(ctx.author)
    await first.children[1].callback(interaction)   # Double

    interaction.response.edit_message.assert_awaited_once_with(view=None)
    assert await _economy.get_balance(84) == bal_after_first - 2 * 2000
    views = _result_views(ctx)
    assert len(views) == 2
    assert [b.stake for b in views[1].children] == [2000, 4000]
    assert views[1].children[0].label == "Flip Again · 2 × 2,000 🪙"
    # Same side carried over: with random → heads, tails still loses.
    assert "0/2" in ctx.channel.send.call_args_list[-1].kwargs["embed"].description
    for v in views:
        v.stop()


@pytest.mark.asyncio
async def test_flip_again_click_replays_same_stake(db, monkeypatch):
    cog = FlipCog(bot=_StubBot())
    ctx = _ctx(uid=85)
    await _economy.add_balance(85, 100_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await cog.cmd_flip.callback(cog, ctx, amount="1000")
    first = _result_views(ctx)[0]
    bal_after_first = await _economy.get_balance(85)

    await first.children[0].callback(_FakeInteraction(ctx.author))   # Flip Again

    assert await _economy.get_balance(85) == bal_after_first - 1000
    views = _result_views(ctx)
    assert len(views) == 2
    assert [b.stake for b in views[1].children] == [1000, 2000]
    for v in views:
        v.stop()
