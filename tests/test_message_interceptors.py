"""Tier B: events.on_message interceptors + handler-chain ordering.

Companion to test_message_effects.py (which covers the side-effect chain:
xp/ragebait/mock/tax/curse/auto_daily). This file covers the *interceptor*
half of on_message — the three short-circuit handlers that consume the
message and the dispatcher logic that runs them in order:

- _handle_blackjack_input — `hit`/`stand`/`double` (with or without the
  `!`) while a blackjack game is active for the author in this channel.
- _handle_puzzle_answer — bare-text answer matching state.active_puzzles.
- _handle_hangman_guess — single-letter free-text guess.

When any interceptor returns True, the AI-routing tail must NOT fire.

The handler chain itself is exercised end-to-end by calling
`cog.on_message(msg)` rather than the helpers directly — so the
dispatcher's order and short-circuit semantics get covered too.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.economy as _economy
import src.events as _events
from src.events import EventsCog

from tests.fakes.discord import FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()
        self.cogs = {}
        self.process_commands_calls = []

    async def fetch_user(self, uid):
        return type("U", (), {"display_name": f"user_{uid}", "id": uid})()

    async def process_commands(self, message):
        self.process_commands_calls.append(message)


class _Channel:
    def __init__(self, ch_id: int = 100):
        self.id = ch_id
        self.send = AsyncMock()


class _Msg:
    """Stand-in for discord.Message that on_message touches."""
    def __init__(self, author, content, guild, channel):
        self.author = author
        self.content = content
        self.guild = guild
        self.channel = channel
        self.mentions = []
        self.reference = None
        self.reply = AsyncMock()


@pytest.fixture
def bot():
    return _StubBot()


@pytest.fixture
def cog(bot):
    return EventsCog(bot)


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    """Stub the side-effect-y collaborators so on_message body runs cleanly."""
    async def _ollama_up():
        return True
    monkeypatch.setattr(_events, "check_ollama_connected", _ollama_up)

    async def _no_rage(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_passive_ragebait", _no_rage)

    async def _no_xp(*a, **kw):
        return (0, False)
    monkeypatch.setattr(_events, "_grant_xp", _no_xp)

    # Pre-claim today's auto-daily for any uid we touch so the auto-daily
    # branch doesn't credit unexpected coins.
    async def _no_daily(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_auto_daily", _no_daily)


# ── _handle_blackjack_input ───────────────────────────────────────────────────
# Pre-conditions for the interceptor: uid in active_blackjack_games AND the
# game's channel_id matches AND the message text is one of "hit"/"stand"
# (with or without "!"). The interceptor short-circuits on_message before
# the AI-routing tail fires.

def _make_bj_game(channel_id: int, deck=None, player_hand=None, dealer_hand=None, amount=100):
    """Bare-minimum game dict for the blackjack interceptor."""
    return {
        "channel_id": channel_id,
        "deck": deck if deck is not None else [{"rank": "5", "suit": "♠"} for _ in range(52)],
        "player_hand": player_hand if player_hand is not None else [{"rank": "10", "suit": "♥"}, {"rank": "5", "suit": "♦"}],
        "dealer_hand": dealer_hand if dealer_hand is not None else [{"rank": "10", "suit": "♣"}, {"rank": "7", "suit": "♣"}],
        "amount": amount,
    }


async def test_blackjack_hit_under_21_continues_game(db, cog):
    player = FakeMember(uid=5001, display_name="player")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=800)
    # Player at 15. Deck top = 5 → bumps to 20 (no bust, no blackjack).
    _state.active_blackjack_games[player.id] = _make_bj_game(
        channel_id=800,
        player_hand=[{"rank": "10", "suit": "♥"}, {"rank": "5", "suit": "♦"}],
    )

    await cog.on_message(_Msg(player, "hit", guild, channel))

    # Game still active, hand grew by one card.
    assert player.id in _state.active_blackjack_games
    assert len(_state.active_blackjack_games[player.id]["player_hand"]) == 3
    # Channel got the "hit again or stand" embed.
    channel.send.assert_awaited()


async def test_blackjack_hit_to_bust_removes_game_and_charges(db, cog):
    """Player at 20, draws a 5, busts at 25. Game removed; bust embed sent."""
    player = FakeMember(uid=5002, display_name="busted")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=801)
    await _economy.add_balance(player.id, 1000)
    _state.active_blackjack_games[player.id] = _make_bj_game(
        channel_id=801,
        player_hand=[{"rank": "K", "suit": "♥"}, {"rank": "10", "suit": "♦"}],  # 20
        amount=100,
    )

    await cog.on_message(_Msg(player, "hit", guild, channel))

    assert player.id not in _state.active_blackjack_games
    channel.send.assert_awaited()
    embed = channel.send.call_args.kwargs["embed"]
    assert "Bust" in embed.title


async def test_blackjack_stand_triggers_dealer_play(db, cog, monkeypatch):
    """`stand` routes to blackjack_stand (shared with the Stand button), which
    plays out the dealer's hand and pays out. Stub it to confirm we got there
    with the author's own channel."""
    player = FakeMember(uid=5003)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=802)
    _state.active_blackjack_games[player.id] = _make_bj_game(channel_id=802)

    stand_calls = []
    async def _stub_stand(author, ch, g):
        stand_calls.append((author.id, ch.id, g.id))
    monkeypatch.setattr(_events, "blackjack_stand", _stub_stand)

    await cog.on_message(_Msg(player, "stand", guild, channel))

    assert stand_calls == [(player.id, 802, 42)]


async def test_blackjack_with_bang_prefix_also_intercepts(db, cog, monkeypatch):
    """`!hit` (with the bang) is the same code path as `hit` — both intercept."""
    player = FakeMember(uid=5004)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=803)
    _state.active_blackjack_games[player.id] = _make_bj_game(channel_id=803)

    stand_calls = []
    async def _stub_stand(author, ch, g):
        stand_calls.append(author.id)
    monkeypatch.setattr(_events, "blackjack_stand", _stub_stand)

    await cog.on_message(_Msg(player, "!stand", guild, channel))

    assert stand_calls == [player.id]


async def test_blackjack_typed_double_routes_to_double_down(db, cog, monkeypatch):
    """`double` is the typed form of the Double Down button — same action."""
    player = FakeMember(uid=5007)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=804)
    _state.active_blackjack_games[player.id] = _make_bj_game(channel_id=804)

    double_calls = []
    async def _stub_double(author, ch, g):
        double_calls.append((author.id, ch.id))
    monkeypatch.setattr(_events, "blackjack_double", _stub_double)

    msg = _Msg(player, "double", guild, channel)
    await cog.on_message(msg)

    assert double_calls == [(player.id, 804)]
    assert msg not in cog.bot.process_commands_calls   # consumed


async def test_blackjack_hit_in_other_channel_does_not_intercept(db, cog):
    """Game registered for channel 900; message arrives in channel 901 →
    interceptor returns False, dispatcher falls through to AI routing."""
    player = FakeMember(uid=5005)
    guild = FakeGuild(gid=42)
    other_channel = _Channel(ch_id=901)
    _state.active_blackjack_games[player.id] = _make_bj_game(channel_id=900)

    msg = _Msg(player, "hit", guild, other_channel)
    await cog.on_message(msg)

    # Game state untouched in either channel.
    assert player.id in _state.active_blackjack_games
    assert len(_state.active_blackjack_games[player.id]["player_hand"]) == 2
    # Dispatcher fell through to the AI-routing tail; it's a non-mention,
    # non-DM, non-AI-thread message, so process_commands runs.
    assert msg in cog.bot.process_commands_calls


async def test_non_hit_stand_text_does_not_intercept(db, cog):
    """`hello` is not hit/stand — the interceptor passes; dispatcher continues."""
    player = FakeMember(uid=5006)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=802)
    _state.active_blackjack_games[player.id] = _make_bj_game(channel_id=802)

    msg = _Msg(player, "hello", guild, channel)
    await cog.on_message(msg)

    # Game untouched.
    assert player.id in _state.active_blackjack_games
    assert len(_state.active_blackjack_games[player.id]["player_hand"]) == 2


# ── _handle_puzzle_answer ─────────────────────────────────────────────────────
# Conditions: cid in active_puzzles AND not "!"-prefixed AND (no invited_ids
# OR uid in invited_ids) AND _norm_puzzle_answer matches.

async def test_puzzle_correct_answer_pays_and_clears(db, cog):
    solver = FakeMember(uid=6001, display_name="solver")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=900)
    _state.active_puzzles[900] = {
        "answer": "echo",
        "reward": 250,
        "user_id": solver.id,
        "invited_ids": None,  # public puzzle
    }

    await cog.on_message(_Msg(solver, "echo", guild, channel))

    assert 900 not in _state.active_puzzles
    assert await _economy.get_balance(solver.id) == 250
    channel.send.assert_awaited()
    embed = channel.send.call_args.kwargs["embed"]
    assert "Correct" in embed.title


async def test_puzzle_normalization_accepts_case_and_whitespace(db, cog):
    """_norm_puzzle_answer collapses case and trailing whitespace."""
    solver = FakeMember(uid=6002)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=901)
    _state.active_puzzles[901] = {
        "answer": "PYTHON",
        "reward": 100,
        "user_id": solver.id,
        "invited_ids": None,
    }

    await cog.on_message(_Msg(solver, "  python  ", guild, channel))

    assert 901 not in _state.active_puzzles
    assert await _economy.get_balance(solver.id) == 100


async def test_puzzle_wrong_answer_leaves_state_intact(db, cog):
    solver = FakeMember(uid=6003)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=902)
    _state.active_puzzles[902] = {
        "answer": "echo",
        "reward": 250,
        "user_id": solver.id,
        "invited_ids": None,
    }

    await cog.on_message(_Msg(solver, "shadow", guild, channel))

    # Puzzle still active, no payout.
    assert 902 in _state.active_puzzles
    assert await _economy.get_balance(solver.id) == 0


async def test_puzzle_uninvited_user_cannot_solve(db, cog):
    """When invited_ids is set, only those users can claim the reward."""
    host = FakeMember(uid=6004)
    randomer = FakeMember(uid=6005)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=903)
    _state.active_puzzles[903] = {
        "answer": "echo",
        "reward": 250,
        "user_id": host.id,
        "invited_ids": {host.id},  # only host
    }

    await cog.on_message(_Msg(randomer, "echo", guild, channel))

    # Puzzle still active; randomer wasn't paid.
    assert 903 in _state.active_puzzles
    assert await _economy.get_balance(randomer.id) == 0


async def test_puzzle_bang_prefix_skips_intercept(db, cog):
    """`!echo` starts with bang → puzzle interceptor declines."""
    solver = FakeMember(uid=6006)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=904)
    _state.active_puzzles[904] = {
        "answer": "echo",
        "reward": 250,
        "user_id": solver.id,
        "invited_ids": None,
    }

    await cog.on_message(_Msg(solver, "!echo", guild, channel))

    # Puzzle still active.
    assert 904 in _state.active_puzzles


# ── _handle_hangman_guess ─────────────────────────────────────────────────────
# Conditions: cid in active_hangman_games AND not "!"-prefixed AND single
# alphabetic character.

async def test_hangman_single_letter_routes_to_process_guess(db, cog, monkeypatch):
    """The interceptor delegates to _process_hangman_guess with the lowered
    single letter. Stub the helper so we just verify routing."""
    guesser = FakeMember(uid=7001, display_name="guesser")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1000)
    _state.active_hangman_games[1000] = {
        "word": "python", "user_id": guesser.id,
        "guessed_letters": set(), "wrong_guesses": 0,
    }

    process_calls = []
    async def _stub_process(channel_arg, uid, cid, guess, name):
        process_calls.append((uid, cid, guess, name))
    monkeypatch.setattr(_events, "_process_hangman_guess", _stub_process)

    await cog.on_message(_Msg(guesser, "P", guild, channel))

    # Single uppercase letter normalized to lowercase, routed in.
    assert process_calls == [(guesser.id, 1000, "p", "guesser")]


async def test_hangman_multi_char_does_not_intercept(db, cog, monkeypatch):
    """Free-text only handles single letters; full-word guesses go through
    `!guess` (or whatever command, not this interceptor)."""
    guesser = FakeMember(uid=7002)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1001)
    _state.active_hangman_games[1001] = {
        "word": "python", "user_id": guesser.id,
        "guessed_letters": set(), "wrong_guesses": 0,
    }

    process_calls = []
    async def _stub_process(*a, **kw):
        process_calls.append(a)
    monkeypatch.setattr(_events, "_process_hangman_guess", _stub_process)

    await cog.on_message(_Msg(guesser, "python", guild, channel))

    # Not intercepted (multi-char).
    assert process_calls == []


async def test_hangman_non_alpha_does_not_intercept(db, cog, monkeypatch):
    guesser = FakeMember(uid=7003)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1002)
    _state.active_hangman_games[1002] = {
        "word": "python", "user_id": guesser.id,
        "guessed_letters": set(), "wrong_guesses": 0,
    }

    process_calls = []
    async def _stub_process(*a, **kw):
        process_calls.append(a)
    monkeypatch.setattr(_events, "_process_hangman_guess", _stub_process)

    await cog.on_message(_Msg(guesser, "1", guild, channel))

    assert process_calls == []


async def test_hangman_no_active_game_does_not_intercept(db, cog, monkeypatch):
    """Single-letter message in a channel with NO hangman game just
    falls through; no _process_hangman_guess call."""
    guesser = FakeMember(uid=7004)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1003)
    # No game registered.

    process_calls = []
    async def _stub_process(*a, **kw):
        process_calls.append(a)
    monkeypatch.setattr(_events, "_process_hangman_guess", _stub_process)

    await cog.on_message(_Msg(guesser, "a", guild, channel))

    assert process_calls == []


# ── Dispatcher / interceptor ordering ─────────────────────────────────────────
# When an interceptor returns True, the AI-routing tail (which calls
# self.bot.process_commands) must NOT fire. When all interceptors decline,
# the tail does fire.

async def test_interceptor_short_circuits_ai_routing(db, cog):
    """Puzzle answer consumed → process_commands not invoked at the tail."""
    solver = FakeMember(uid=8001)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1100)
    _state.active_puzzles[1100] = {
        "answer": "echo",
        "reward": 100,
        "user_id": solver.id,
        "invited_ids": None,
    }

    msg = _Msg(solver, "echo", guild, channel)
    await cog.on_message(msg)

    # Interceptor consumed the message; AI tail did NOT call process_commands.
    assert msg not in cog.bot.process_commands_calls


async def test_no_interceptor_falls_through_to_process_commands(db, cog):
    """Plain text in a normal channel → AI routing tail hands off to
    process_commands so !commands still dispatch normally."""
    user = FakeMember(uid=8002)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1101)

    msg = _Msg(user, "just chatting", guild, channel)
    await cog.on_message(msg)

    assert msg in cog.bot.process_commands_calls


async def test_bot_message_short_circuits_immediately(db, cog):
    """`if message.author == self.bot.user: return` — no handlers run."""
    bot_user = cog.bot.user  # same .id as self.bot.user
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1102)

    # Stash counters; they should not move.
    before_msgs = _state.stats_messages_seen
    before_today = _state.stats_messages_today

    msg = _Msg(bot_user, "internal echo", guild, channel)
    await cog.on_message(msg)

    # Stats counters untouched (would have incremented for any non-bot msg).
    assert _state.stats_messages_seen == before_msgs
    assert _state.stats_messages_today == before_today
    # process_commands not invoked.
    assert msg not in cog.bot.process_commands_calls


async def test_stats_counters_increment_for_user_message(db, cog):
    """Sanity check: any non-bot message bumps both counters."""
    user = FakeMember(uid=8003)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=1103)

    before_msgs = _state.stats_messages_seen
    before_today = _state.stats_messages_today

    await cog.on_message(_Msg(user, "hi", guild, channel))

    assert _state.stats_messages_seen == before_msgs + 1
    assert _state.stats_messages_today == before_today + 1
