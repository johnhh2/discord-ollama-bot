"""Hit / Stand buttons under a blackjack hand (`BlackjackView`).

Same contract as the play-again buttons on !slots / !flip (pinned in
test_play_again_view.py): only the player can click, one click per set, the
blocklist gate runs on the click path, and the set vanishes on timeout.
Game-specific here: the deal posts a set, a click strips it and posts the
next turn with a fresh one (or the result with none), and a typed `hit` /
`stand` retires the set it bypassed. `!stop` retiring the set is covered in
test_cmd_stop.py; the typed-word routing in test_message_interceptors.py.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.economy as _economy
import src.games.blackjack as bj
from src.games.blackjack import (
    BlackjackCog, BlackjackView, BLACKJACK_BUTTON_TIMEOUT,
    blackjack_hit, blackjack_stand, blackjack_double, can_double,
)
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage

pytestmark = pytest.mark.asyncio

UID = 7


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(),
        )


def _fix_deck(monkeypatch, *ranks: str):
    """Make the next deal draw `ranks` in order: player, dealer, player,
    dealer, then every hit / dealer draw. (draw_card pops from the end.)"""
    cards = [{"rank": r, "suit": "♠"} for r in reversed(ranks)]
    monkeypatch.setattr(bj, "new_deck", lambda: list(cards))


def _views(ctx) -> list:
    """Every view attached to a message in ctx.channel, in order."""
    return [c.kwargs["view"] for c in ctx.channel.send.call_args_list
            if c.kwargs.get("view") is not None]


def _embeds(ctx) -> list:
    """Every embed sent to ctx.channel, in order: the deal, then each turn or
    the result — and a record announcement can follow a winning result, so
    index from the front rather than taking the last one."""
    return [c.kwargs["embed"] for c in ctx.channel.send.call_args_list
            if c.kwargs.get("embed") is not None]


def _buttons(view) -> dict:
    return {b.label: b for b in view.children}


async def _deal(monkeypatch, *ranks: str, amount: str = "100", balance: int = 10_000) -> FakeCtx:
    """Fund the player, fix the deck, and run `!blackjack <amount>`."""
    ctx = FakeCtx(author=FakeMember(uid=UID, display_name="player"), guild=FakeGuild(gid=42))
    await _economy.add_balance(UID, balance)
    _fix_deck(monkeypatch, *ranks)
    cog = BlackjackCog(bot=None)
    await cog.cmd_blackjack.callback(cog, ctx, amount=amount)
    return ctx


# ── The deal ──────────────────────────────────────────────────────────────────

async def test_deal_offers_hit_and_stand_buttons(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7")   # player 15 vs dealer 9 + hidden

    views = _views(ctx)
    assert len(views) == 1
    view = views[0]
    assert isinstance(view, BlackjackView)
    assert [b.label for b in view.children] == ["Hit", "Stand", "Double Down · 100 🪙"]
    assert [b.style for b in view.children] == [
        discord.ButtonStyle.primary, discord.ButtonStyle.success, discord.ButtonStyle.danger,
    ]
    assert view.timeout == BLACKJACK_BUTTON_TIMEOUT == 60.0
    assert view.message is not None                         # on_timeout strips them
    assert _state.active_blackjack_games[UID]["view"] is view
    # Typing still works after the buttons expire.
    assert "type `hit`, `stand`, or `double`" in _embeds(ctx)[0].description
    view.stop()


async def test_natural_blackjack_settles_at_once_with_no_buttons(db, monkeypatch):
    ctx = await _deal(monkeypatch, "A", "K", "9", "7")

    assert UID not in _state.active_blackjack_games
    assert _views(ctx) == []
    assert await _economy.get_balance(UID) == 10_000 - 100 + 250


async def test_declined_bet_deals_nothing(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", balance=50)

    assert UID not in _state.active_blackjack_games
    assert _views(ctx) == []
    assert ctx.channel.send.await_count == 0   # the refusal went through ctx.send


# ── Clicks ────────────────────────────────────────────────────────────────────

async def test_hit_click_draws_a_card_and_posts_a_fresh_set(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "3")   # hit → 18
    first = _views(ctx)[0]
    interaction = _FakeInteraction(ctx.author)

    await _buttons(first)["Hit"].callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(view=None)
    assert first.is_finished()
    game = _state.active_blackjack_games[UID]
    assert [c["rank"] for c in game["player_hand"]] == ["10", "5", "3"]
    views = _views(ctx)
    assert len(views) == 2 and views[1] is not first
    assert game["view"] is views[1]
    # Three cards now: the first decision is behind us, so no Double Down.
    assert [b.label for b in views[1].children] == ["Hit", "Stand"]
    assert not can_double(game)
    assert "(18)" in _embeds(ctx)[1].description
    assert "type `hit` / `stand`" in _embeds(ctx)[1].description
    views[1].stop()


async def test_hit_click_to_bust_ends_the_hand_with_no_buttons(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "K")   # hit → 25

    await _buttons(_views(ctx)[0])["Hit"].callback(_FakeInteraction(ctx.author))

    assert UID not in _state.active_blackjack_games
    assert len(_views(ctx)) == 1
    assert "Bust" in _embeds(ctx)[1].title
    assert await _economy.get_balance(UID) == 10_000 - 100
    # A bust is a loss for the gambling P/L graph, same as losing on a stand.
    assert _state.gambling_today_by_user[(42, str(UID))]["lost"] == 100


async def test_hit_click_to_21_stands_automatically(db, monkeypatch):
    # player 10+5, hit 6 → 21; dealer 9+7 must draw: K → 26, bust.
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", "K")

    await _buttons(_views(ctx)[0])["Hit"].callback(_FakeInteraction(ctx.author))

    assert UID not in _state.active_blackjack_games
    assert len(_views(ctx)) == 1
    assert "wins" in _embeds(ctx)[1].description
    assert await _economy.get_balance(UID) == 10_000 - 100 + 200


async def test_stand_click_plays_the_dealer_and_settles(db, monkeypatch):
    # player K+9 = 19; dealer 9+7 = 16 draws a 2 → 18. Player wins.
    ctx = await _deal(monkeypatch, "K", "9", "9", "7", "2")
    first = _views(ctx)[0]
    interaction = _FakeInteraction(ctx.author)

    await _buttons(first)["Stand"].callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(view=None)
    assert first.is_finished()
    assert UID not in _state.active_blackjack_games
    assert len(_views(ctx)) == 1
    result = _embeds(ctx)[1]
    assert "Dealer (18)" in result.description
    assert "wins" in result.description
    assert await _economy.get_balance(UID) == 10_000 - 100 + 200


async def test_click_after_the_hand_ended_only_drops_the_buttons(db, monkeypatch):
    """The strip after a typed `stand` / `!stop` can fail; a click on the
    leftover set must not act on a hand that's gone (or on the next one)."""
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "3")
    first = _views(ctx)[0]
    _state.active_blackjack_games.pop(UID)
    interaction = _FakeInteraction(ctx.author)

    await _buttons(first)["Hit"].callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(view=None)
    assert first.is_finished()
    assert len(_views(ctx)) == 1
    assert ctx.channel.send.await_count == 1   # nothing new posted


async def test_second_click_on_the_same_set_is_ignored(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "3", "2")
    first = _views(ctx)[0]
    await _buttons(first)["Hit"].callback(_FakeInteraction(ctx.author))
    second = _FakeInteraction(ctx.author)

    await _buttons(first)["Hit"].callback(second)

    second.response.defer.assert_awaited_once()
    second.response.edit_message.assert_not_awaited()
    assert len(_state.active_blackjack_games[UID]["player_hand"]) == 3   # one card, not two
    _views(ctx)[-1].stop()


async def test_other_user_is_rejected_ephemerally(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7")
    first = _views(ctx)[0]
    intruder = _FakeInteraction(FakeMember(uid=8, display_name="intruder"))

    await _buttons(first)["Stand"].callback(intruder)

    intruder.response.send_message.assert_awaited_once()
    assert intruder.response.send_message.call_args.kwargs == {"ephemeral": True}
    intruder.response.edit_message.assert_not_awaited()
    assert UID in _state.active_blackjack_games
    assert not first.is_finished()   # the player can still click
    first.stop()


async def test_player_banned_after_the_deal_cannot_click(db, monkeypatch):
    """Blocklist gate on the click path — no message passes on_message."""
    ctx = await _deal(monkeypatch, "10", "5", "9", "7")
    first = _views(ctx)[0]
    _state.blocklist[(42, UID)] = {"reason": "t", "banned_by": 1, "banned_at": None}
    interaction = _FakeInteraction(ctx.author)
    try:
        await _buttons(first)["Stand"].callback(interaction)
    finally:
        _state.blocklist.pop((42, UID), None)

    interaction.response.defer.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()
    assert UID in _state.active_blackjack_games
    first.stop()


# ── Typed input alongside the buttons ─────────────────────────────────────────

async def test_typed_hit_retires_the_previous_set_and_posts_a_fresh_one(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "3")
    first = _views(ctx)[0]

    await blackjack_hit(ctx.author, ctx.channel, ctx.guild)

    assert first.is_finished()
    first.message.edit.assert_awaited_once_with(view=None)
    views = _views(ctx)
    assert len(views) == 2 and views[1] is not first
    assert _state.active_blackjack_games[UID]["view"] is views[1]
    views[1].stop()


async def test_typed_stand_retires_the_set_before_settling(db, monkeypatch):
    ctx = await _deal(monkeypatch, "K", "9", "9", "7", "2")
    first = _views(ctx)[0]

    await blackjack_stand(ctx.author, ctx.channel, ctx.guild)

    assert first.is_finished()
    first.message.edit.assert_awaited_once_with(view=None)
    assert UID not in _state.active_blackjack_games
    assert len(_views(ctx)) == 1


async def test_pending_hand_is_not_playable(db):
    """A hand claimed but not yet charged (shop_charge in flight) ignores
    hit/stand from either path."""
    author = FakeMember(uid=UID, display_name="player")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    _state.active_blackjack_games[UID] = {
        "amount": 100, "channel_id": ctx.channel.id, "pending": True,
        "deck": [{"rank": "3", "suit": "♠"}],
        "player_hand": [{"rank": "10", "suit": "♠"}, {"rank": "5", "suit": "♠"}],
        "dealer_hand": [{"rank": "9", "suit": "♠"}, {"rank": "7", "suit": "♠"}],
    }

    await blackjack_hit(author, ctx.channel, ctx.guild)
    await blackjack_stand(author, ctx.channel, ctx.guild)
    await blackjack_double(author, ctx.channel, ctx.guild)

    assert len(_state.active_blackjack_games[UID]["player_hand"]) == 2
    assert _state.active_blackjack_games[UID]["amount"] == 100
    assert ctx.channel.send.await_count == 0


# ── Double down ───────────────────────────────────────────────────────────────
# Offered on the first decision only (two cards, nothing drawn): a second bet
# equal to the first, exactly one card, then the hand stands — or busts.

def _yielding_charge(monkeypatch):
    """Make shop_charge yield to the event loop before charging, so two
    actions racing through the double's charge window really interleave
    (the sqlite fake is synchronous — see CLAUDE.md)."""
    real = bj.shop_charge

    async def _charge(ctx, uid, cost, **kw):
        await asyncio.sleep(0)
        return await real(ctx, uid, cost, **kw)
    monkeypatch.setattr(bj, "shop_charge", _charge)


def _settlements(ctx) -> list:
    """Result embeds — the ones that reveal the dealer's total."""
    return [e for e in _embeds(ctx) if "Dealer (" in e.description]


async def test_double_click_charges_draws_one_card_and_stands(db, monkeypatch):
    # player 10+5, doubles into a 6 → 21; dealer 9+7 must draw: K → bust.
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", "K")
    first = _views(ctx)[0]
    interaction = _FakeInteraction(ctx.author)

    await _buttons(first)["Double Down · 100 🪙"].callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(view=None)
    assert first.is_finished()
    assert UID not in _state.active_blackjack_games
    assert len(_views(ctx)) == 1                   # the hand is over: no new set
    result = _settlements(ctx)[0]
    assert "10♠  5♠  6♠" in result.description     # exactly one card drawn
    assert "Doubled down — stake **200 🪙**" in result.description
    assert "wins **200 🪙**" in result.description
    # Paid 100 + 100, collected 400 on the doubled stake.
    assert await _economy.get_balance(UID) == 10_000 - 200 + 400
    assert _state.gambling_today_by_user[(42, str(UID))]["gained"] == 200


async def test_double_click_can_bust_on_its_one_card(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "K")   # 15 + K = 25

    await _buttons(_views(ctx)[0])["Double Down · 100 🪙"].callback(_FakeInteraction(ctx.author))

    assert UID not in _state.active_blackjack_games
    bust = _embeds(ctx)[1]
    assert "Bust" in bust.title
    assert "Doubled down — stake **200 🪙**" in bust.description
    assert "loses **200 🪙**" in bust.description
    assert await _economy.get_balance(UID) == 10_000 - 200
    assert _state.gambling_today_by_user[(42, str(UID))]["lost"] == 200


async def test_double_pushes_refund_the_doubled_stake(db, monkeypatch):
    # player 10+5 doubles into a 6 → 21; dealer 9+7 draws a 5 → 21. Push.
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", "5")

    await blackjack_double(ctx.author, ctx.channel, ctx.guild)

    assert "Push" in _settlements(ctx)[0].description
    assert await _economy.get_balance(UID) == 10_000


async def test_declined_double_leaves_the_hand_and_reposts_its_buttons(db, monkeypatch):
    """Bet 100 from 150 leaves 50: the second bet is refused. The hand is
    untouched (two cards, original stake) and gets a fresh set — the
    clicked one was already stripped."""
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", balance=150)
    first = _views(ctx)[0]

    await _buttons(first)["Double Down · 100 🪙"].callback(_FakeInteraction(ctx.author))

    game = _state.active_blackjack_games[UID]
    assert len(game["player_hand"]) == 2
    assert game["amount"] == 100 and not game.get("doubled")
    assert "pending" not in game
    assert await _economy.get_balance(UID) == 50
    assert "Insufficient Funds" in _embeds(ctx)[1].title
    views = _views(ctx)
    assert len(views) == 2 and views[1] is not first
    assert game["view"] is views[1]
    assert [b.label for b in views[1].children] == ["Hit", "Stand", "Double Down · 100 🪙"]
    views[1].stop()


async def test_typed_double_after_a_hit_is_refused(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "3")
    await blackjack_hit(ctx.author, ctx.channel, ctx.guild)     # 18, three cards
    sent_before = ctx.channel.send.await_count

    await blackjack_double(ctx.author, ctx.channel, ctx.guild)

    game = _state.active_blackjack_games[UID]
    assert len(game["player_hand"]) == 3 and game["amount"] == 100
    assert await _economy.get_balance(UID) == 10_000 - 100
    assert ctx.channel.send.await_count == sent_before + 1
    assert "first two cards" in _embeds(ctx)[-1].description
    game["view"].stop()


async def test_hand_is_locked_while_the_second_bet_is_charged(db, monkeypatch):
    """A stand (button or typed) landing while the double's charge is in
    flight must not settle the hand at the old stake: it sees the hand as
    pending and does nothing; the double completes alone."""
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", "K")
    _yielding_charge(monkeypatch)

    await asyncio.gather(
        blackjack_double(ctx.author, ctx.channel, ctx.guild),
        blackjack_stand(ctx.author, ctx.channel, ctx.guild),
    )

    assert UID not in _state.active_blackjack_games
    assert len(_settlements(ctx)) == 1
    assert "Doubled down" in _settlements(ctx)[0].description
    assert await _economy.get_balance(UID) == 10_000 - 200 + 400


async def test_two_doubles_at_once_charge_once(db, monkeypatch):
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", "K")
    _yielding_charge(monkeypatch)

    await asyncio.gather(
        blackjack_double(ctx.author, ctx.channel, ctx.guild),
        blackjack_double(ctx.author, ctx.channel, ctx.guild),
    )

    assert len(_settlements(ctx)) == 1
    assert "10♠  5♠  6♠" in _settlements(ctx)[0].description   # one card, not two
    assert await _economy.get_balance(UID) == 10_000 - 200 + 400


async def test_stop_during_the_charge_refunds_the_second_bet(db, monkeypatch):
    """!stop forfeits the first bet; a second bet that landed during the
    forfeit never joined a hand and goes straight back."""
    ctx = await _deal(monkeypatch, "10", "5", "9", "7", "6", "K")
    real = bj.shop_charge

    async def _stop_mid_charge(ctx_, uid, cost, **kw):
        _state.active_blackjack_games.pop(uid)      # !stop lands here
        return await real(ctx_, uid, cost, **kw)
    monkeypatch.setattr(bj, "shop_charge", _stop_mid_charge)

    await blackjack_double(ctx.author, ctx.channel, ctx.guild)

    assert UID not in _state.active_blackjack_games
    assert _settlements(ctx) == []
    assert await _economy.get_balance(UID) == 10_000 - 100


# ── Lifetime ──────────────────────────────────────────────────────────────────

async def test_timeout_removes_the_buttons_from_the_message():
    view = BlackjackView(FakeMember(uid=UID), None, FakeGuild(gid=42))
    view.message = FakeMessage()

    await view.on_timeout()

    view.message.edit.assert_awaited_once_with(view=None)


async def test_timeout_after_a_click_leaves_the_message_alone(db, monkeypatch):
    """The click already stripped the buttons via the interaction edit."""
    ctx = await _deal(monkeypatch, "K", "9", "9", "7", "2")
    first = _views(ctx)[0]
    await _buttons(first)["Stand"].callback(_FakeInteraction(ctx.author))

    await first.on_timeout()

    first.message.edit.assert_not_awaited()


async def test_retire_survives_a_deleted_message():
    view = BlackjackView(FakeMember(uid=UID), None, FakeGuild(gid=42))
    view.message = FakeMessage()
    gone = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
    view.message.edit = AsyncMock(side_effect=gone)

    await view.retire()   # must not raise

    assert view.is_finished()


async def test_retire_during_the_send_strips_once_the_message_lands():
    """A concurrent action can retire a set whose send is still in flight;
    attach() finishes the job so no live-looking buttons are left behind."""
    view = BlackjackView(FakeMember(uid=UID), None, FakeGuild(gid=42))
    await view.retire()
    msg = FakeMessage()

    await view.attach(msg)

    msg.edit.assert_awaited_once_with(view=None)
