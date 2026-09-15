"""The "biggest gambling loss" record (`gambling_loss`): one per-guild row the
house games — flip, slots, blackjack and the bot race — compete for with the
net a losing bet cost the player.

Pins:
- a lost !flip offers the stake; a smaller loss leaves the record alone, a
  bigger one takes it, and each change announces exactly once
- a tie keeps the incumbent (plain first-come-wins, like the payout records)
- a multi-coin flip offers the batch's net, not the per-coin stake
- wins never touch it; godmode losses never compete
- record_exclude shrinks the offer the way it does for the payout records
- slots, blackjack (bust, dealer win, doubled-down bust) and the bot race all
  feed it, each with its own detail line
- !records renders the row with the detail line
"""
import random

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.games.race as _race
import src.games.blackjack as bj
from src.cogs.economy_cog import EconomyCog
from src.economy import (
    GAMBLING_LOSS_RECORD, format_loss_record_detail, try_set_loss_record,
)
from src.gambling.flip import FlipCog, play_flip
from src.gambling.slots import play_slots
from src.games.blackjack import BlackjackCog, blackjack_hit, blackjack_stand, blackjack_double
from src.games.race import play_bot_race
from src.persistence import load_records

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

pytestmark = pytest.mark.asyncio

GID = 42
UID = 7
BOT_ID = 999_999_999


class _StubBot:
    def __init__(self):
        self.user = type("U", (), {"id": BOT_ID})()


def _ctx(uid: int = UID, name: str = "player") -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(uid=uid, display_name=name), guild=FakeGuild(gid=GID))
    ctx.bot = _StubBot()
    return ctx


def _embed_titles(ctx) -> list[str]:
    return [c.kwargs["embed"].title for c in ctx.channel.send.call_args_list
            if c.kwargs.get("embed") is not None]


def _loss_announcements(ctx) -> list[str]:
    """Descriptions of every biggest-gambling-loss announcement in the channel."""
    return [
        c.kwargs["embed"].description
        for c in ctx.channel.send.call_args_list
        if c.kwargs.get("embed") is not None
        and c.kwargs["embed"].title == "🏆 New Record!"
        and "biggest gambling loss" in c.kwargs["embed"].description
    ]


async def _loss_record() -> dict | None:
    return (await load_records(GID)).get(GAMBLING_LOSS_RECORD)


async def _flip(ctx, amount: str, n: int = 1, side: str = "heads") -> None:
    cog = FlipCog(bot=ctx.bot)
    await cog.cmd_flip.callback(cog, ctx, amount=amount, n=n, side=side)


# ── the helper and its detail line ───────────────────────────────────────────

async def test_detail_line_renders_each_game():
    assert format_loss_record_detail({"game": "flip", "bet": 200_000}) == "Flip • Bet: 200,000 🪙"
    assert format_loss_record_detail({"game": "flip", "bet": 1_000, "coins": 4}) == "Flip • Bet: 4 × 1,000 🪙"
    assert format_loss_record_detail(
        {"game": "slots", "bet": 25, "symbols": "⬛ | 🍋 | 🔔"}
    ) == "Slots • Bet: 25 🪙 • Symbols: ⬛ | 🍋 | 🔔"
    assert format_loss_record_detail(
        {"game": "blackjack", "bet": 100, "player_hand": "10♠  5♠", "player_score": 15, "dealer_score": 17}
    ) == "Blackjack • Bet: 100 🪙 • Hand: 10♠  5♠ (15) • Dealer: 17"
    # A bust never sees the dealer play, so there is no dealer score to show.
    assert format_loss_record_detail(
        {"game": "blackjack", "bet": 100, "player_hand": "10♠  5♠  K♠", "player_score": 25}
    ) == "Blackjack • Bet: 100 🪙 • Hand: 10♠  5♠  K♠ (25)"
    assert format_loss_record_detail({"game": "race", "bet": 500}) == "Race • Bet: 500 🪙"
    # A row from before any field existed still renders.
    assert format_loss_record_detail({}) == "Gamble"


async def test_helper_refuses_dms_zero_losses_and_godmode(db):
    assert await try_set_loss_record(None, UID, "player", 1_000, game="flip") is False
    assert await try_set_loss_record(GID, UID, "player", 0, game="flip") is False
    _state.godmode_users.add(UID)
    assert await try_set_loss_record(GID, UID, "player", 1_000, game="flip") is False
    assert await _loss_record() is None


# ── !flip ─────────────────────────────────────────────────────────────────────

async def test_losing_flip_sets_the_record_to_the_stake(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 500_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)   # tails → heads loses

    await _flip(ctx, "200k")

    rec = await _loss_record()
    assert rec["value"] == 200_000
    assert rec["holder_id"] == UID
    assert rec["holder_name"] == "player"
    assert rec["game"] == "flip"
    assert rec["bet"] == 200_000
    assert _loss_announcements(ctx) == [
        "**player** just set a new biggest gambling loss record: **200,000 🪙**\n"
        "*Flip • Bet: 200,000 🪙*"
    ]
    # The announcement follows the result embed, never precedes it.
    titles = _embed_titles(ctx)
    assert titles.index("🪙 Tails!") < titles.index("🏆 New Record!")


async def test_smaller_loss_keeps_the_record_and_bigger_loss_takes_it(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 1_000_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await _flip(ctx, "200k")
    await _flip(ctx, "100k")
    assert (await _loss_record())["value"] == 200_000
    assert len(_loss_announcements(ctx)) == 1

    await _flip(ctx, "300k")
    assert (await _loss_record())["value"] == 300_000
    assert len(_loss_announcements(ctx)) == 2


async def test_a_tie_keeps_the_incumbent(db, monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.99)
    first = _ctx(uid=1, name="first")
    await _economy.add_balance(1, 200_000)
    await _flip(first, "200k")

    second = _ctx(uid=2, name="second")
    await _economy.add_balance(2, 200_000)
    await _flip(second, "200k")

    assert (await _loss_record())["holder_name"] == "first"
    assert _loss_announcements(second) == []


async def test_multi_coin_flip_offers_the_batch_net(db, monkeypatch):
    """`!flip 1000 4` with one rigged win: cost 4,000, won 2,000 → net -2,000.
    The record is that net — not the per-coin stake, not the gross cost."""
    ctx = _ctx()
    await _economy.add_balance(UID, 100_000)
    _state.rigged_flips[UID] = 1
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await _flip(ctx, "1000", n=4)

    rec = await _loss_record()
    assert rec["value"] == 2_000
    assert rec["coins"] == 4
    assert rec["bet"] == 1_000
    assert _loss_announcements(ctx)[0].endswith("*Flip • Bet: 4 × 1,000 🪙*")


async def test_winning_flip_never_touches_the_loss_record(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 100_000)
    monkeypatch.setattr(random, "random", lambda: 0.01)   # heads → heads wins

    await _flip(ctx, "50k")

    assert await _loss_record() is None
    assert (await load_records(GID))["flip"]["value"] == 100_000


async def test_godmode_losses_never_compete(db, monkeypatch):
    ctx = _ctx()
    _state.godmode_users.add(UID)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await _flip(ctx, "1m")

    assert await _loss_record() is None
    assert _loss_announcements(ctx) == []


async def test_record_exclude_shrinks_the_offer_like_the_payout_records(db, monkeypatch):
    """The dailies claim passes its property-revenue portion as
    record_exclude: the player really lost 1,000, but only the non-property
    600 competes."""
    ctx = _ctx()
    await _economy.add_balance(UID, 10_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await play_flip(ctx.author, ctx.channel, ctx.guild, 1_000, record_exclude=400)

    assert await _economy.get_balance(UID) == 9_000
    rec = await _loss_record()
    assert rec["value"] == 600
    assert rec["bet"] == 600


async def test_fully_excluded_stake_offers_nothing(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 10_000)
    monkeypatch.setattr(random, "random", lambda: 0.99)

    await play_flip(ctx.author, ctx.channel, ctx.guild, 1_000, record_exclude=1_000)

    assert await _loss_record() is None


# ── !slots ────────────────────────────────────────────────────────────────────

def _force_slots_loss(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.99)         # skip the house spin
    monkeypatch.setattr(random, "choice", lambda seq: "⬛")      # blank reels → no win
    monkeypatch.setattr(random, "sample", lambda seq, k: ["⬛", "⬛", "⬛"])


async def test_losing_slots_spin_sets_the_record(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 100_000)
    _force_slots_loss(monkeypatch)

    await play_slots(ctx.author, ctx.channel, ctx.guild, 25_000)

    rec = await _loss_record()
    assert rec["value"] == 25_000
    assert rec["game"] == "slots"
    assert rec["symbols"] == "⬛ | ⬛ | ⬛"
    assert _loss_announcements(ctx) == [
        "**player** just set a new biggest gambling loss record: **25,000 🪙**\n"
        "*Slots • Bet: 25,000 🪙 • Symbols: ⬛ | ⬛ | ⬛*"
    ]
    titles = _embed_titles(ctx)
    assert titles.index("🎰 No Win") < titles.index("🏆 New Record!")


async def test_slots_record_exclude_applies_to_the_loss(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 100_000)
    _force_slots_loss(monkeypatch)

    await play_slots(ctx.author, ctx.channel, ctx.guild, 1_000, record_exclude=400)

    assert (await _loss_record())["value"] == 600


# ── !blackjack ────────────────────────────────────────────────────────────────

def _fix_deck(monkeypatch, *ranks: str):
    """Deal `ranks` in order: two to the player, two to the dealer, then every
    hit / dealer draw (draw_card pops from the end)."""
    cards = [{"rank": r, "suit": "♠"} for r in reversed(ranks)]
    monkeypatch.setattr(bj, "new_deck", lambda: list(cards))


async def _deal(monkeypatch, *ranks: str, amount: str = "5000") -> FakeCtx:
    ctx = _ctx()
    await _economy.add_balance(UID, 100_000)
    _fix_deck(monkeypatch, *ranks)
    cog = BlackjackCog(bot=None)
    await cog.cmd_blackjack.callback(cog, ctx, amount=amount)
    return ctx


async def test_blackjack_bust_sets_the_record_with_the_hand(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "K")   # 15 vs 16; hit → 25

    await blackjack_hit(ctx.author, ctx.channel, ctx.guild)

    rec = await _loss_record()
    assert rec["value"] == 5_000
    assert rec["game"] == "blackjack"
    assert rec["player_hand"] == "10♠  5♠  K♠"
    assert rec["player_score"] == 25
    assert "dealer_score" not in rec
    assert _loss_announcements(ctx) == [
        "**player** just set a new biggest gambling loss record: **5,000 🪙**\n"
        "*Blackjack • Bet: 5,000 🪙 • Hand: 10♠  5♠  K♠ (25)*"
    ]
    titles = _embed_titles(ctx)
    assert titles.index("💥 Bust!") < titles.index("🏆 New Record!")


async def test_blackjack_dealer_win_sets_the_record_with_both_scores(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "8")   # 15 vs 17: dealer stands and wins

    await blackjack_stand(ctx.author, ctx.channel, ctx.guild)

    rec = await _loss_record()
    assert rec["value"] == 5_000
    assert rec["player_hand"] == "10♠  5♠"
    assert rec["player_score"] == 15
    assert rec["dealer_score"] == 17
    assert _loss_announcements(ctx)[0].endswith(
        "*Blackjack • Bet: 5,000 🪙 • Hand: 10♠  5♠ (15) • Dealer: 17*"
    )


async def test_blackjack_doubled_down_bust_offers_the_doubled_stake(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "8", "K")   # double: 15 + K → 25

    await blackjack_double(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(UID) == 100_000 - 10_000
    rec = await _loss_record()
    assert rec["value"] == 10_000
    assert rec["bet"] == 10_000


async def test_blackjack_push_and_win_leave_the_loss_record_alone(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "7", "9", "8")   # 17 vs 17: push
    await blackjack_stand(ctx.author, ctx.channel, ctx.guild)
    assert await _loss_record() is None

    ctx = await _deal(monkeypatch, "10", "9", "9", "8")   # 19 vs 17: player wins
    await blackjack_stand(ctx.author, ctx.channel, ctx.guild)
    assert await _loss_record() is None


# ── !race @Bot ────────────────────────────────────────────────────────────────

@pytest.fixture
def _bot_race_loss(monkeypatch):
    """Instant ticks, and the bot lane outruns the human every tick."""
    monkeypatch.setattr(_race, "RACE_TICK_SECONDS", 0)
    monkeypatch.setattr(_race, "_tick_rolls", lambda players: {players[0]: 1, players[1]: 3})


async def test_bot_race_loss_sets_the_record(db, _bot_race_loss):
    ctx = _ctx()
    await _economy.add_balance(UID, 10_000)

    await play_bot_race(ctx.author, ctx.channel, ctx.guild, 4_000,
                        FakeMember(uid=BOT_ID, display_name="Bot"))

    assert await _economy.get_balance(UID) == 6_000
    rec = await _loss_record()
    assert rec["value"] == 4_000
    assert rec["game"] == "race"
    assert _loss_announcements(ctx) == [
        "**player** just set a new biggest gambling loss record: **4,000 🪙**\n"
        "*Race • Bet: 4,000 🪙*"
    ]


async def test_bot_race_record_exclude_applies_to_the_loss(db, _bot_race_loss):
    ctx = _ctx()
    await _economy.add_balance(UID, 10_000)

    await play_bot_race(ctx.author, ctx.channel, ctx.guild, 1_000,
                        FakeMember(uid=BOT_ID, display_name="Bot"), record_exclude=400)

    assert (await _loss_record())["value"] == 600


async def test_free_bot_race_offers_nothing(db, _bot_race_loss):
    ctx = _ctx()

    await play_bot_race(ctx.author, ctx.channel, ctx.guild, 0,
                        FakeMember(uid=BOT_ID, display_name="Bot"))

    assert await _loss_record() is None


# ── one row across games ──────────────────────────────────────────────────────

async def test_games_share_one_row_and_the_biggest_loss_wins(db, monkeypatch):
    ctx = _ctx()
    await _economy.add_balance(UID, 1_000_000)
    _force_slots_loss(monkeypatch)                      # also makes flips lose (random → 0.99)

    await play_slots(ctx.author, ctx.channel, ctx.guild, 30_000)
    assert (await _loss_record())["game"] == "slots"

    await _flip(ctx, "20k")                              # smaller: slots keeps it
    assert (await _loss_record())["game"] == "slots"

    await _flip(ctx, "40k")                              # bigger: flip takes it
    rec = await _loss_record()
    assert rec["game"] == "flip"
    assert rec["value"] == 40_000
    assert "symbols" not in rec                         # the old row's meta is gone with it


# ── !records ──────────────────────────────────────────────────────────────────

async def test_records_embed_renders_the_biggest_loss_line(db):
    await _persistence.save_records(GID, {
        GAMBLING_LOSS_RECORD: {
            "value": 200_000, "holder_id": 1, "holder_name": "whale",
            "game": "flip", "bet": 200_000, "coins": 1,
        },
    })
    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))

    await cog.cmd_records.callback(cog, ctx)

    desc = ctx.sent_embeds[-1].description
    assert "**Biggest Loss:** 200,000 🪙 — **whale**\n  ↳ Flip • Bet: 200,000 🪙" in desc


async def test_records_embed_shows_the_loss_line_as_empty_before_any_loss(db):
    cog = EconomyCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=GID))

    await cog.cmd_records.callback(cog, ctx)

    assert "**Biggest Loss:** *none yet*" in ctx.sent_embeds[-1].description
