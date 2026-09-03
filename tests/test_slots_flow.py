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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.gambling.slots as slots_mod
from src.gambling.slots import SlotsCog, apply_jackpot_bonus, play_slots
from src.gambling.play_again import PlayAgainView
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


# ── !unrig ────────────────────────────────────────────────────────────────────

async def _count_rig_rows(uid: int) -> int:
    """Rows across all four rigged_* tables for one user."""
    pool = await _persistence.get_pool()
    total = 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for table in ("rigged_slots", "rigged_flips", "rigged_scratch", "rigged_steal"):
                await cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=%s", (uid,))
                total += (await cur.fetchone())[0]
    return total


@pytest.mark.asyncio
async def test_unrig_clears_all_rigs_for_target_only(db):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=1)

    _state.rigged_slots[5] = "7️⃣"
    _state.rigged_flips[5] = 3
    _state.rigged_scratch[5] = 4
    _state.rigged_steal[5] = 2
    _state.rigged_flips[6] = 1   # bystander's rig must survive
    await _persistence.save_rigged_slots()
    await _persistence.save_rigged_flips()
    await _persistence.save_rigged_scratch()
    await _persistence.save_rigged_steal()

    target = FakeMember(uid=5, display_name="rigged-guy")
    await cog.cmd_unrig.callback(cog, ctx, target=target)

    assert 5 not in _state.rigged_slots
    assert 5 not in _state.rigged_flips
    assert 5 not in _state.rigged_scratch
    assert 5 not in _state.rigged_steal
    assert await _count_rig_rows(5) == 0
    # Bystander untouched, in memory and in the DB.
    assert _state.rigged_flips[6] == 1
    assert await _count_rig_rows(6) == 1
    assert ctx.sent_embeds[-1].title == "🧹 Rigs Cleared"


@pytest.mark.asyncio
async def test_unrig_with_no_active_rigs_reports_not_rigged(db):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=1)

    target = FakeMember(uid=7, display_name="clean-guy")
    await cog.cmd_unrig.callback(cog, ctx, target=target)

    assert ctx.sent_embeds[-1].title == "🧹 Not Rigged"


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

    rec = _state.gambling_today_by_user.get((42, "10"), {})
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
    rec = _state.gambling_today_by_user.get((42, "11"), {})
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

    assert (42, "12") not in _state.gambling_today_by_user


# ── Roll Again button ─────────────────────────────────────────────────────────
#
# A hand-typed !slots result carries one "Roll Again" button that re-spins the
# same bet. The dailies 🎰 claim goes through play_slots' default and gets no
# button — the daily stake is a one-shot. The view's own contract (owner-only,
# one replay per set, blocklist gate, timeout) is pinned in
# test_play_again_view.py.


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(),
        )


def _result_views(ctx) -> list:
    """Every view play_slots attached to a message in ctx.channel, in order.
    Filtered rather than `[-1]` because record announcements can follow the
    result message."""
    return [c.kwargs["view"] for c in ctx.channel.send.call_args_list
            if c.kwargs.get("view") is not None]


def _force_loss(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.01)  # house branch
    monkeypatch.setattr(random, "sample", lambda seq, k: ["🎰", "🍋", "🔔"])


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["loss", "money_back", "win", "jackpot"])
async def test_slots_command_offers_roll_again_on_every_outcome(db, monkeypatch, outcome):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=20)
    await _economy.add_balance(20, 100_000)
    if outcome == "loss":
        _force_loss(monkeypatch)
    elif outcome == "money_back":
        monkeypatch.setattr(random, "random", lambda: 0.01)
        monkeypatch.setattr(random, "sample", lambda seq, k: ["🍒", "🍋", "🔔"])
    elif outcome == "win":
        _state.rigged_slots[20] = "🍒"
    else:
        _state.rigged_slots[20] = "7️⃣"

    await cog.cmd_slots.callback(cog, ctx, amount="1000")

    views = _result_views(ctx)
    assert len(views) == 1
    view = views[0]
    assert isinstance(view, PlayAgainView)
    assert view.message is not None          # on_timeout strips the button
    assert [b.stake for b in view.children] == [1000]
    assert view.children[0].label == "Roll Again · 1,000 🪙"
    view.stop()


@pytest.mark.asyncio
async def test_slots_declined_bet_offers_no_roll_again(db, monkeypatch):
    """Insufficient funds sends only the refusal — no result, no button."""
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=21)
    await _economy.add_balance(21, 500)
    _force_loss(monkeypatch)

    await cog.cmd_slots.callback(cog, ctx, amount="1000")

    assert _result_views(ctx) == []


@pytest.mark.asyncio
async def test_play_slots_default_has_no_roll_again(db, monkeypatch):
    """play_slots' default (the dailies 🎰 claim path) attaches no button."""
    ctx = _ctx(uid=22)
    await _economy.add_balance(22, 100_000)
    _force_loss(monkeypatch)

    await play_slots(ctx.author, ctx.channel, ctx.guild, 1000)

    assert ctx.channel.send.await_count == 1
    assert _result_views(ctx) == []


@pytest.mark.asyncio
async def test_roll_again_click_respins_same_bet_and_offers_another(db, monkeypatch):
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=23)
    await _economy.add_balance(23, 100_000)
    _force_loss(monkeypatch)

    await cog.cmd_slots.callback(cog, ctx, amount="1000")
    first = _result_views(ctx)[0]
    bal_after_first = await _economy.get_balance(23)
    jackpot_after_first = _state.slot_jackpot

    interaction = _FakeInteraction(ctx.author)
    await first.children[0].callback(interaction)

    # Button dropped from the first result, bet charged again, pot fed again.
    interaction.response.edit_message.assert_awaited_once_with(view=None)
    assert await _economy.get_balance(23) == bal_after_first - 1000
    assert _state.slot_jackpot == jackpot_after_first + max(1, int(1000 * SLOT_JACKPOT_CONTRIB))
    views = _result_views(ctx)
    assert len(views) == 2
    assert views[1] is not first
    assert [b.stake for b in views[1].children] == [1000]
    for v in views:
        v.stop()


# ── Sub-max-bet jackpot-bonus note ────────────────────────────────────────────
#
# A bet under SLOT_JACKPOT_BONUS_MAX_BET gets a footnote saying it forfeits
# part of the progressive bonus — once per user per bot restart (the seen-set
# is in-memory on purpose).


def _result_desc(ctx) -> str:
    """Description of the first message sent — the spin result (a forced loss
    sets no record, so nothing follows it)."""
    return ctx.channel.send.call_args_list[0].kwargs["embed"].description


def _stop_views(ctx):
    for v in _result_views(ctx):
        v.stop()


@pytest.mark.asyncio
async def test_sub_max_bet_note_shows_once_per_boot_per_user(db, monkeypatch):
    monkeypatch.setattr(slots_mod, "_jackpot_bonus_noted", set())
    cog = SlotsCog(bot=_StubBot())
    _force_loss(monkeypatch)
    for uid in (30, 31):
        await _economy.add_balance(uid, 100_000)

    ctx = _ctx(uid=30)
    await cog.cmd_slots.callback(cog, ctx, amount=str(SLOT_JACKPOT_BONUS_MAX_BET - 1))
    first = _result_desc(ctx)
    assert f"Bets under **{SLOT_JACKPOT_BONUS_MAX_BET:,} 🪙**" in first
    assert f"**{SLOT_JACKPOT_BONUS_MAX_MULT:.0f}x** progressive jackpot bonus" in first
    # The note brings a second button that jumps straight to the full-bonus bet.
    view = _result_views(ctx)[0]
    assert [b.stake for b in view.children] == [SLOT_JACKPOT_BONUS_MAX_BET - 1, SLOT_JACKPOT_BONUS_MAX_BET]
    assert view.children[1].label == f"Standard · {SLOT_JACKPOT_BONUS_MAX_BET:,} 🪙"
    _stop_views(ctx)

    # Same user, same boot: no repeat, and back to the single button.
    ctx = _ctx(uid=30)
    await cog.cmd_slots.callback(cog, ctx, amount="500")
    assert "Bets under" not in _result_desc(ctx)
    assert [b.stake for b in _result_views(ctx)[0].children] == [500]
    _stop_views(ctx)

    # Another user gets their own.
    ctx = _ctx(uid=31)
    await cog.cmd_slots.callback(cog, ctx, amount="500")
    assert "Bets under" in _result_desc(ctx)
    assert [b.stake for b in _result_views(ctx)[0].children] == [500, SLOT_JACKPOT_BONUS_MAX_BET]
    _stop_views(ctx)


@pytest.mark.asyncio
async def test_full_bonus_button_spins_at_max_bet(db, monkeypatch):
    monkeypatch.setattr(slots_mod, "_jackpot_bonus_noted", set())
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=35)
    await _economy.add_balance(35, 100_000)
    _force_loss(monkeypatch)

    await cog.cmd_slots.callback(cog, ctx, amount="500")
    first = _result_views(ctx)[0]
    bal_after_first = await _economy.get_balance(35)

    await first.children[1].callback(_FakeInteraction(ctx.author))   # Standard

    assert await _economy.get_balance(35) == bal_after_first - SLOT_JACKPOT_BONUS_MAX_BET
    views = _result_views(ctx)
    assert len(views) == 2
    # The max-bet result has no note (bet isn't under the threshold) and so
    # only the plain Roll Again.
    assert [b.stake for b in views[1].children] == [SLOT_JACKPOT_BONUS_MAX_BET]
    assert "Bets under" not in ctx.channel.send.call_args_list[-1].kwargs["embed"].description
    _stop_views(ctx)


@pytest.mark.asyncio
async def test_dailies_path_gets_the_note_but_no_buttons(db, monkeypatch):
    """play_slots' default (the dailies 🎰 claim) still shows the footnote
    but never a button — the daily stake is a one-shot."""
    monkeypatch.setattr(slots_mod, "_jackpot_bonus_noted", set())
    ctx = _ctx(uid=36)
    await _economy.add_balance(36, 100_000)
    _force_loss(monkeypatch)

    await play_slots(ctx.author, ctx.channel, ctx.guild, 500)

    assert "Bets under" in _result_desc(ctx)
    assert _result_views(ctx) == []


@pytest.mark.asyncio
async def test_max_bet_or_more_gets_no_note_and_keeps_it_pending(db, monkeypatch):
    monkeypatch.setattr(slots_mod, "_jackpot_bonus_noted", set())
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=32)
    await _economy.add_balance(32, 100_000)
    _force_loss(monkeypatch)

    await cog.cmd_slots.callback(cog, ctx, amount=str(SLOT_JACKPOT_BONUS_MAX_BET))

    assert "Bets under" not in _result_desc(ctx)
    assert 32 not in slots_mod._jackpot_bonus_noted   # a later small bet still gets it
    _stop_views(ctx)


@pytest.mark.asyncio
async def test_first_spin_hint_and_bonus_note_stack(db, monkeypatch):
    """A brand-new player betting small sees both footnotes on one result."""
    monkeypatch.setattr(slots_mod, "_jackpot_bonus_noted", set())
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=33)
    await _economy.add_balance(33, 100_000)
    _force_loss(monkeypatch)

    await cog.cmd_slots.callback(cog, ctx, amount="500")

    desc = _result_desc(ctx)
    assert "!slotsrewards" in desc
    assert "Bets under" in desc
    _stop_views(ctx)


@pytest.mark.asyncio
async def test_declined_bet_does_not_consume_the_note(db, monkeypatch):
    monkeypatch.setattr(slots_mod, "_jackpot_bonus_noted", set())
    cog = SlotsCog(bot=_StubBot())
    ctx = _ctx(uid=34)
    await _economy.add_balance(34, 100)   # can't cover the bet

    await cog.cmd_slots.callback(cog, ctx, amount="500")

    assert 34 not in slots_mod._jackpot_bonus_noted
