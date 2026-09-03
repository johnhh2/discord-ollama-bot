"""Notification policy for gambling/game/interaction messages.

Rule: a message triggered by a user's own action (a gamble, a move, a claim
click) must be sent silent — that user is already watching the channel.
The only loud sends are ones whose job is to ping someone who *isn't* the
actor: game invites, and record announcements from the scheduled lottery
draw (notify=True).

These tests pin the policy at its three most regression-prone points:
announce_record's holder ping, the raw-channel gambling result sends
(which bypass SilentContext), and the invite send (which must opt OUT of
SilentContext's silent default and carry its mentions in content, since
embed mentions never notify).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.economy as _economy
from src.helpers import announce_record, emb, C_BLUE
from src.gambling.flip import play_flip
from src.games.chess import _bump_board
from src.invites import _wait_for_confirmations

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeThread


class _RecordingChannel:
    """Bare channel fake that records every send's (args, kwargs)."""
    def __init__(self, guild=None, ch_id: int = 100):
        self.id = ch_id  # games report results with channel_id=channel.id
        self.guild = guild
        self.sent: list[tuple[tuple, dict]] = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return None


@pytest.mark.asyncio
async def test_announce_record_holder_ping_is_silent_by_default(db):
    """The achiever normally just gambled in this channel — mention for the
    highlight, but no push notification."""
    chan = _RecordingChannel(guild=FakeGuild(gid=42))
    await announce_record(chan, "flip", "Joseph", 5_000, holder_id=77)

    assert len(chan.sent) == 1
    _, kwargs = chan.sent[0]
    assert kwargs.get("content") == "<@77>"
    assert kwargs.get("silent") is True


@pytest.mark.asyncio
async def test_announce_record_notify_pings_loud(db):
    """notify=True (lottery draw — holder isn't present) keeps a real ping."""
    chan = _RecordingChannel(guild=FakeGuild(gid=42))
    await announce_record(chan, "lottery", "Joseph", 5_000, holder_id=77, notify=True)

    _, kwargs = chan.sent[0]
    assert kwargs.get("content") == "<@77>"
    assert kwargs.get("silent") is False


@pytest.mark.asyncio
async def test_play_flip_result_is_silent(db):
    """Gambling results go through raw channel.send (no SilentContext) — the
    silent flag must be explicit."""
    author = FakeMember(uid=77, display_name="player")
    guild = FakeGuild(gid=42)
    chan = _RecordingChannel(guild=guild)
    await _economy.add_balance(77, 10_000)

    await play_flip(author, chan, guild, 100)

    assert chan.sent, "flip should announce its result"
    for _, kwargs in chan.sent:
        assert kwargs.get("silent") is True


class _BumpChannel(_RecordingChannel):
    """_bump_board reads .id off the returned message — hand back stubs."""
    _next_id = 0

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        self._next_id += 1
        return SimpleNamespace(id=self._next_id)


@pytest.mark.asyncio
async def test_chess_turn_ping_rides_on_the_board_message_loud(db):
    """A PvP turn ping is the content of the board-image message itself,
    with silent=False — one message carries the board AND the "@X's turn!"
    line, so the thread's channel-list preview reads whose turn it is and
    the next player (who didn't act) gets a real ping."""
    chan = _BumpChannel()
    game: dict = {}
    board_file = object()  # _bump_board just forwards it to send()
    await _bump_board(
        chan, game, emb("♟️ Chess", "turn", C_BLUE),
        file=board_file, turn_content="<@2>'s turn!", ping=True,
    )

    # The embed goes first, silent.
    _, embed_kwargs = chan.sent[0]
    assert embed_kwargs.get("silent") is True
    # The board message is LAST: image + turn content, loud.
    board_args, board_kwargs = chan.sent[-1]
    assert board_args == ("<@2>'s turn!",)
    assert board_kwargs.get("file") is board_file
    assert board_kwargs.get("silent") is False
    assert game["board_msg_id"] is not None
    assert game["turn_msg_id"] is None  # no standalone turn message


@pytest.mark.asyncio
async def test_chess_bot_game_turn_line_is_silent(db):
    """Bot games carry the same turn line on the board message for the
    thread preview, but silent — the lone human just moved."""
    chan = _BumpChannel()
    game: dict = {}
    await _bump_board(
        chan, game, emb("♟️ Chess", "turn", C_BLUE),
        file=object(), turn_content="Maia (1300)'s turn!",
    )

    board_args, board_kwargs = chan.sent[-1]
    assert board_args == ("Maia (1300)'s turn!",)
    assert board_kwargs.get("silent") is True


@pytest.mark.asyncio
async def test_chess_turn_line_falls_back_to_own_message_without_board(db):
    """When the board render failed there's no image message to carry the
    turn line — it goes out standalone, keeping the ping."""
    chan = _BumpChannel()
    game: dict = {}
    await _bump_board(
        chan, game, emb("♟️ Chess", "turn", C_BLUE),
        turn_content="<@2>'s turn!", ping=True,
    )

    turn_args, turn_kwargs = chan.sent[-1]
    assert turn_args == ("<@2>'s turn!",)
    assert turn_kwargs.get("silent") is False
    assert game["board_msg_id"] is None
    assert game["turn_msg_id"] is not None


@pytest.mark.asyncio
async def test_chess_bump_without_ping_is_silent(db):
    """Board bumps with no turn line (game-over) stay silent and send no
    extra message."""
    chan = _BumpChannel()
    game: dict = {}
    await _bump_board(chan, game, emb("♟️ Chess", "turn", C_BLUE))

    for args, kwargs in chan.sent:
        assert kwargs.get("silent") is True
        assert not args
    assert game["turn_msg_id"] is None


@pytest.mark.asyncio
async def test_chess_board_appends_in_thread_without_deleting(db):
    """In a game thread the board pair just appends — no fetch/delete of the
    prior pair. The thread is dedicated to the game, and skipping the extra
    round-trips posts the new board faster."""
    thread = FakeThread(thread_id=777)
    thread.fetch_message = AsyncMock()
    game = {"embed_msg_id": 11, "board_msg_id": 12}

    await _bump_board(thread, game, emb("♟️ Chess", "turn", C_BLUE))

    thread.fetch_message.assert_not_awaited()
    thread.send.assert_awaited()


@pytest.mark.asyncio
async def test_chess_board_still_deletes_prior_pair_in_channel(db):
    """Legacy in-channel games (started before game threads, or thread
    creation failed) keep post-then-delete so the shared channel doesn't
    fill with stale boards."""
    deleted: list[int] = []

    class _Chan(_BumpChannel):
        async def fetch_message(self, mid):
            async def _del():
                deleted.append(mid)
            return SimpleNamespace(id=mid, delete=_del)

    chan = _Chan()
    game = {"embed_msg_id": 11, "board_msg_id": 12, "turn_msg_id": 13}

    await _bump_board(chan, game, emb("♟️ Chess", "turn", C_BLUE))

    assert sorted(deleted) == [11, 12, 13]


@pytest.mark.asyncio
async def test_invite_send_is_loud_with_content_mentions(db):
    """Invites are the flip side of the policy: the invitee hasn't acted yet,
    so the invite must override SilentContext's silent default AND put the
    mentions in content (embed mentions never notify)."""
    ctx = FakeCtx(author=FakeMember(uid=1, display_name="host"), guild=FakeGuild(gid=42))

    class _Bot:
        async def wait_for(self, *a, **kw):
            raise asyncio.TimeoutError

    ctx.bot = _Bot()
    invitee = FakeMember(uid=2, display_name="guest")

    await _wait_for_confirmations(ctx, [invitee], timeout=0.01)

    args, kwargs = ctx.send_mock.call_args
    assert kwargs.get("silent") is False
    # content is passed positionally through FakeCtx.send
    content = args[0] if args else kwargs.get("content")
    assert content == invitee.mention
