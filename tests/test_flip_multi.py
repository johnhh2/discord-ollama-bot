"""!flip <amount> [n] — multi-coin behavior.

Pins the parts that are easy to silently regress when refactoring:
- total cost is amount * n (charged once via shop_charge)
- each rigged-flip entry is consumed per-coin (single-use, not per command)
- net P/L and final balance reflect every coin
"""
import random

import pytest

import src.state as _state
import src.economy as _economy
from src.gambling.flip import FlipCog

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

    # Gambling event recorded the signed net (gained=bet, since net > 0).
    rec = _state.gambling_today_by_user.get("77", {})
    assert rec.get("gained") == bet
    assert rec.get("lost", 0) == 0
