"""Tier D: !slots integration flow.

eval_slots is well covered by the original suite. This file pins the
parts NOT covered there:

- apply_jackpot_bonus: scaling math (extracted helper, pure function).
- Jackpot accumulation: every spin adds SLOT_JACKPOT_CONTRIB × bet
  to state.slot_jackpot and persists.
- Rigging: state.rigged_slots forces a guaranteed three-of-a-kind and
  decrements (single-use per entry).
- Jackpot win resets state.slot_jackpot to SLOT_JACKPOT_SEED.
"""
import random

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.gambling.slots import SlotsCog, apply_jackpot_bonus
from src.config import (
    SLOT_JACKPOT_SEED, SLOT_JACKPOT_CONTRIB, SLOT_JACKPOT_BONUS_MIN_BET,
    SLOT_JACKPOT_BONUS_MAX_BET, SLOT_JACKPOT_BONUS_MAX_MULT, SLOT_MIN_BET,
    SLOT_MULT_3CHERRY,
)

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

# No module-level pytestmark — TestApplyJackpotBonus is sync, the rest are
# async. Async tests get @pytest.mark.asyncio per-function below.


class _StubBot:
    def __init__(self):
        self.user = type("U", (), {"id": 999_999_999})()


# ── apply_jackpot_bonus (pure helper) ─────────────────────────────────────────

class TestApplyJackpotBonus:
    def test_min_bet_returns_unscaled_jackpot(self):
        # At the min bet, multiplier is exactly 1× (modulo float→int floor).
        assert apply_jackpot_bonus(10_000, SLOT_JACKPOT_BONUS_MIN_BET) == 10_000

    def test_max_bet_returns_max_multiplier(self):
        assert apply_jackpot_bonus(10_000, SLOT_JACKPOT_BONUS_MAX_BET) == int(
            10_000 * SLOT_JACKPOT_BONUS_MAX_MULT
        )

    def test_above_max_bet_clamps_at_max_multiplier(self):
        # Bets above the max-bet cap don't keep scaling.
        big = apply_jackpot_bonus(10_000, SLOT_JACKPOT_BONUS_MAX_BET * 100)
        assert big == int(10_000 * SLOT_JACKPOT_BONUS_MAX_MULT)

    def test_below_min_bet_returns_unscaled(self):
        # Bets below SLOT_JACKPOT_BONUS_MIN_BET still get exactly 1× (no negative scaling).
        if SLOT_JACKPOT_BONUS_MIN_BET > 0:
            assert apply_jackpot_bonus(10_000, 0) == 10_000

    def test_midpoint_bet_scales_proportionally(self):
        """At a bet halfway between min and max (by bet value), the multiplier
        should be halfway between 1× and max."""
        bet = (SLOT_JACKPOT_BONUS_MIN_BET + SLOT_JACKPOT_BONUS_MAX_BET) // 2
        ratio = (bet - SLOT_JACKPOT_BONUS_MIN_BET) / (
            SLOT_JACKPOT_BONUS_MAX_BET - SLOT_JACKPOT_BONUS_MIN_BET
        )
        expected_mult = 1.0 + ratio * (SLOT_JACKPOT_BONUS_MAX_MULT - 1.0)
        assert apply_jackpot_bonus(10_000, bet) == int(10_000 * expected_mult)

    def test_jackpot_zero_returns_zero(self):
        assert apply_jackpot_bonus(0, 100) == 0

    def test_monotonic_in_bet(self):
        """Higher bets within the scaling range produce higher prizes."""
        prev = apply_jackpot_bonus(10_000, SLOT_JACKPOT_BONUS_MIN_BET)
        step = max(1, (SLOT_JACKPOT_BONUS_MAX_BET - SLOT_JACKPOT_BONUS_MIN_BET) // 10)
        for bet in range(SLOT_JACKPOT_BONUS_MIN_BET + step, SLOT_JACKPOT_BONUS_MAX_BET + 1, step):
            curr = apply_jackpot_bonus(10_000, bet)
            assert curr >= prev
            prev = curr


# ── Jackpot accumulation ──────────────────────────────────────────────────────

async def _read_jackpot() -> int:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT jackpot FROM slots_jackpot WHERE id=1")
            row = await cur.fetchone()
    return row[0] if row else 0


def _ctx(uid: int = 1, balance: int = 100_000):
    """Build a slots-friendly FakeCtx and pre-fund the user."""
    author = FakeMember(uid=uid, display_name="player")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    return ctx


@pytest.mark.asyncio
async def test_slots_spin_adds_to_jackpot_and_persists(db, monkeypatch):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=1)
    await _economy.add_balance(1, 100_000)

    starting_jackpot = SLOT_JACKPOT_SEED
    _state.slot_jackpot = starting_jackpot

    # Force a non-rigged, non-house spin that produces no win for clarity.
    monkeypatch.setattr(random, "random", lambda: 0.99)         # skip house
    monkeypatch.setattr(random, "choice", lambda seq: "⬛")      # blank reels → no win
    monkeypatch.setattr(random, "sample", lambda seq, k: ["⬛", "⬛", "⬛"])

    bet = 1000
    await cog.cmd_slots.callback(cog, ctx, amount=str(bet))

    expected_contrib = max(1, int(bet * SLOT_JACKPOT_CONTRIB))
    assert _state.slot_jackpot == starting_jackpot + expected_contrib
    assert await _read_jackpot() == starting_jackpot + expected_contrib


@pytest.mark.asyncio
async def test_slots_min_bet_below_threshold_rejected(db):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=2)
    await _economy.add_balance(2, 100_000)

    starting_jackpot = _state.slot_jackpot

    await cog.cmd_slots.callback(cog, ctx, amount=str(SLOT_MIN_BET - 1))

    # Balance untouched, jackpot untouched.
    assert await _economy.get_balance(2) == 100_000
    assert _state.slot_jackpot == starting_jackpot


# ── Rigging ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slots_rigged_forces_three_of_a_kind_and_decrements(db, monkeypatch):
    """state.rigged_slots[uid] = symbol forces reels = [sym, sym, sym] and
    pops the entry so it's single-use."""
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=3)
    await _economy.add_balance(3, 100_000)
    _state.rigged_slots[3] = "🍒"   # forces three cherries

    bet = 100
    bal_before = await _economy.get_balance(3)
    await cog.cmd_slots.callback(cog, ctx, amount=str(bet))

    # Three cherries pays 3CHERRY multiplier.
    expected_winnings = bet * SLOT_MULT_3CHERRY
    # Balance: -bet (charged via shop_charge) + winnings.
    assert await _economy.get_balance(3) == bal_before - bet + expected_winnings
    # Rigging entry consumed.
    assert 3 not in _state.rigged_slots


def test_slots_house_edge_is_positive_at_10k_bet():
    """Simulate many slot spins at a 10k bet; verify expected gross return
    is below the bet (house edge > 0). Uses a fixed seed so the result
    is reproducible.

    Models the production reel logic from cmd_slots:
    - 5% chance of a forced loss (random.sample of distinct non-blank
      symbols, guaranteed no match)
    - 95% chance of three independent random.choice(SLOT_REEL) draws

    Excludes the progressive jackpot from the EV calculation: in the
    long-run steady state, the progressive pot is funded by all spins
    (2% contribution) and paid back to players when the jackpot hits,
    so it nets out near zero. We check the underlying eval_slots-driven
    EV, which is the actual house edge.
    """
    import random as _random
    from src.gambling.slots import eval_slots
    from src.config import (
        SLOT_REEL, SLOT_HOUSE_CHANCE, SLOT_MULT_JACKPOT,
    )

    rng = _random.Random(12345)
    bet = 10_000
    spins = 200_000
    distinct_non_blank = [s for s in dict.fromkeys(SLOT_REEL) if s != "⬛"]

    total_gross = 0
    for _ in range(spins):
        if rng.random() < SLOT_HOUSE_CHANCE:
            reels = rng.sample(distinct_non_blank, 3)
        else:
            reels = [rng.choice(SLOT_REEL) for _ in range(3)]
        label, mult = eval_slots(reels, bet)

        if label == "jackpot":
            # Production pays progressive pot here; for a steady-state EV
            # estimate we treat the jackpot as paying SLOT_MULT_JACKPOT × bet
            # (the table multiplier). The progressive pot is funded by
            # spin contributions and is approximately EV-neutral.
            total_gross += bet * SLOT_MULT_JACKPOT
        elif label == "1cherry":
            total_gross += bet  # money back
        else:
            total_gross += bet * mult

    avg_return = total_gross / spins
    house_edge = 1.0 - avg_return / bet

    # Sanity: house must keep some edge.
    assert avg_return < bet, (
        f"Slots EV at {bet:,} bet is {avg_return:,.2f} (house edge "
        f"{house_edge:+.4%}); expected < bet."
    )
    # Loose upper-bound sanity to catch wildly off configurations.
    assert house_edge < 0.50, (
        f"House edge {house_edge:.4%} is implausibly high — payout table "
        f"may have regressed."
    )

    # Surface the value so it shows up in test output if --tb=short or -v.
    print(f"\n[slots EV] bet={bet:,} spins={spins:,} "
          f"avg_gross={avg_return:,.2f} house_edge={house_edge:+.4%}")


@pytest.mark.asyncio
async def test_slots_rigged_jackpot_pays_progressive_pot_and_resets(db, monkeypatch):
    """Rigging with the jackpot symbol triggers the progressive-jackpot
    branch, paying state.slot_jackpot × bonus and resetting to seed."""
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=4)
    await _economy.add_balance(4, 100_000)

    # Pre-load the progressive jackpot above the seed.
    _state.slot_jackpot = 50_000
    await _persistence.save_jackpot(50_000)
    _state.rigged_slots[4] = "7️⃣"   # forces 3-sevens jackpot

    bet = SLOT_JACKPOT_BONUS_MAX_BET   # max bonus multiplier
    bal_before = await _economy.get_balance(4)
    await cog.cmd_slots.callback(cog, ctx, amount=str(bet))

    # Jackpot accumulates the 2% contribution BEFORE the jackpot check fires
    # (line ordering in cmd_slots), so the pre-bonus pot is 50_000 + contrib.
    expected_pot = 50_000 + max(1, int(bet * SLOT_JACKPOT_CONTRIB))
    expected_prize = int(expected_pot * SLOT_JACKPOT_BONUS_MAX_MULT)

    assert await _economy.get_balance(4) == bal_before - bet + expected_prize
    # Pot reset to seed.
    assert _state.slot_jackpot == SLOT_JACKPOT_SEED
    assert await _read_jackpot() == SLOT_JACKPOT_SEED
    # Rigging entry consumed.
    assert 4 not in _state.rigged_slots


# ── Gambling-event recording (powers !graph gambling) ────────────────────────
#
# Pin the side-effect that test_gambling_graph.py can't catch on its own:
# cmd_slots actually CALLS record_gambling_event at every outcome branch
# (no-win → lost; normal win → gained=net; jackpot → gained=net). If a
# refactor drops one, the unit-level "record_gambling_event aggregates"
# tests still pass, but !graph gambling silently goes blank in prod.


@pytest.mark.asyncio
async def test_slots_no_win_records_gambling_lost(db, monkeypatch):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=10)
    await _economy.add_balance(10, 100_000)

    # Force a guaranteed-loss spin: house-edge branch with three DISTINCT
    # non-cherry, non-blank symbols (no match, no cherry retention).
    monkeypatch.setattr(random, "random", lambda: 0.01)  # < SLOT_HOUSE_CHANCE
    monkeypatch.setattr(random, "sample", lambda seq, k: ["🎰", "🍋", "🔔"])

    bet = 1000
    await cog.cmd_slots.callback(cog, ctx, amount=str(bet))

    rec = _state.gambling_today_by_user.get("10", {})
    assert rec.get("lost") == bet
    assert rec.get("gained", 0) == 0


@pytest.mark.asyncio
async def test_slots_three_cherry_win_records_gambling_gained_net(db, monkeypatch):
    """A 3🍒 win pays SLOT_MULT_3CHERRY × bet. Net P/L = winnings - bet."""
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=11)
    await _economy.add_balance(11, 100_000)
    _state.rigged_slots[11] = "🍒"   # forces three cherries

    bet = 100
    await cog.cmd_slots.callback(cog, ctx, amount=str(bet))

    expected_winnings = bet * SLOT_MULT_3CHERRY
    expected_net = expected_winnings - bet
    rec = _state.gambling_today_by_user.get("11", {})
    assert rec.get("gained") == expected_net
    assert rec.get("lost", 0) == 0


@pytest.mark.asyncio
async def test_slots_godmode_user_doesnt_record_gambling(db, monkeypatch):
    """Godmode bypasses shop_charge (no real bet deduction), so the recorder
    must skip too — otherwise the graph would show fictional P/L."""
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=12)
    await _economy.add_balance(12, 100_000)
    _state.godmode_users.add(12)

    monkeypatch.setattr(random, "random", lambda: 0.01)
    monkeypatch.setattr(random, "sample", lambda seq, k: ["🍒", "🍋", "🔔"])

    await cog.cmd_slots.callback(cog, ctx, amount="1000")

    assert "12" not in _state.gambling_today_by_user
