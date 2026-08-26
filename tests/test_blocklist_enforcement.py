"""Regression tests for the blocklist bypasses found in the 2026-08 audit.

Two holes existed, both from the same root cause: the `!ban` / `!globalban`
check lived only in `events.on_message`, so anything that reaches the bot by
another route walked straight past it.

1. **DMs.** The per-guild check was `message.guild is not None and (gid, uid)
   in blocklist`. A DM has no guild, so a banned user could DM the bot and keep
   playing. Because balances have no guild dimension, they farmed coins in a DM
   and spent them in the server that banned them.

2. **Reactions.** Six listeners act on raw reactions; only `dailies_cog`
   mirrored the check. A banned user could still claim `!event` coin drops,
   file bounty claims (and collect the poll-voter reward), and play wagered
   TTT/C4 by clicking number reactions.

`src.permissions.is_silenced` is now the single source of truth. Any new entry
point that can act on a user's behalf must consult it.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.events as _events
from src.events import EventsCog
from src.permissions import is_silenced
from src.cogs.economy_cog import EconomyCog

from tests.fakes.discord import FakeMember, FakeGuild

pytestmark = pytest.mark.asyncio

GUILD_A = 111
GUILD_B = 222
BANNED = 4242
CLEAN = 5353


def _ban(guild_id: int, uid: int) -> None:
    _state.blocklist[(guild_id, uid)] = {
        "reason": "test", "banned_by": 1, "banned_at": None,
    }


class _BotUser:
    id = 999_999_999


class _StubBot:
    def __init__(self):
        self.user = _BotUser()
        self.cogs = {}
        self.process_commands_calls = []

    async def process_commands(self, message):
        self.process_commands_calls.append(message)

    async def fetch_user(self, uid):
        return type("U", (), {"display_name": f"user_{uid}", "id": uid})()


class _Channel:
    def __init__(self, ch_id: int = 100):
        self.id = ch_id
        self.send = AsyncMock()


class _Msg:
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


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    """Silence the collaborators on_message fans out to."""
    async def _up():
        return True
    monkeypatch.setattr(_events, "check_ollama_connected", _up)

    async def _no_xp(*a, **kw):
        return (0, False)
    monkeypatch.setattr(_events, "_grant_xp", _no_xp)

    async def _none(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_passive_ragebait", _none)
    monkeypatch.setattr(_events, "_auto_daily", _none)


# ── is_silenced semantics ─────────────────────────────────────────────────────

async def test_is_silenced_scopes_guild_bans_to_their_guild():
    _ban(GUILD_A, BANNED)
    assert is_silenced(BANNED, GUILD_A) is True
    assert is_silenced(BANNED, GUILD_B) is False
    assert is_silenced(CLEAN, GUILD_A) is False


async def test_is_silenced_covers_dms_for_anyone_banned_anywhere():
    """A DM has no guild to scope to, and the economy is global — so a ban in
    any guild silences DMs. This is the fix for bypass #1."""
    assert is_silenced(BANNED, None) is False
    _ban(GUILD_A, BANNED)
    assert is_silenced(BANNED, None) is True
    assert is_silenced(CLEAN, None) is False


async def test_is_silenced_honours_the_global_blocklist_everywhere():
    _state.global_blocklist[BANNED] = {"reason": "t", "banned_by": 1, "banned_at": None}
    assert is_silenced(BANNED, GUILD_A) is True
    assert is_silenced(BANNED, GUILD_B) is True
    assert is_silenced(BANNED, None) is True


# ── bypass 1: DMs ─────────────────────────────────────────────────────────────

async def test_guild_ban_blocks_commands_in_that_guild(bot):
    cog = EventsCog(bot)
    guild = FakeGuild(GUILD_A)
    member = FakeMember(BANNED, "Banned")
    _ban(GUILD_A, BANNED)

    await cog.on_message(_Msg(member, "!daily", guild, _Channel()))
    assert bot.process_commands_calls == []


async def test_guild_ban_also_blocks_the_same_user_in_dms(bot):
    """The bypass: DM had no guild, so the per-guild key was never checked."""
    cog = EventsCog(bot)
    member = FakeMember(BANNED, "Banned")
    _ban(GUILD_A, BANNED)

    await cog.on_message(_Msg(member, "!daily", None, _Channel(101)))
    assert bot.process_commands_calls == []


async def test_unbanned_users_are_unaffected_in_dms(bot):
    cog = EventsCog(bot)
    member = FakeMember(CLEAN, "Clean")
    _ban(GUILD_A, BANNED)

    await cog.on_message(_Msg(member, "!daily", None, _Channel(101)))
    assert len(bot.process_commands_calls) == 1


async def test_ban_in_one_guild_does_not_silence_another_guild(bot):
    """`!ban` stays per-guild for guild traffic — only DMs widen."""
    cog = EventsCog(bot)
    member = FakeMember(BANNED, "Banned")
    _ban(GUILD_A, BANNED)

    await cog.on_message(_Msg(member, "!daily", FakeGuild(GUILD_B), _Channel()))
    assert len(bot.process_commands_calls) == 1


# ── bypass 2: reactions ───────────────────────────────────────────────────────

def _event_reaction(msg_id: int, guild):
    return type("R", (), {
        "message": type("M", (), {"id": msg_id, "guild": guild})(),
        "emoji": "🪙",
    })()


async def test_banned_user_cannot_claim_an_event_coin_drop(bot):
    from src.economy import get_balance

    cog = EconomyCog(bot)
    guild = FakeGuild(GUILD_A)
    _ban(GUILD_A, BANNED)
    _state.active_events[555] = {"amount": 10_000, "rewarded": set()}
    user = type("U", (), {"id": BANNED, "bot": False})()

    before = await get_balance(BANNED)
    await cog.on_reaction_add(_event_reaction(555, guild), user)
    assert await get_balance(BANNED) == before


async def test_globally_banned_user_cannot_claim_an_event_coin_drop(bot):
    from src.economy import get_balance

    cog = EconomyCog(bot)
    _state.global_blocklist[BANNED] = {"reason": "t", "banned_by": 1, "banned_at": None}
    _state.active_events[556] = {"amount": 10_000, "rewarded": set()}
    user = type("U", (), {"id": BANNED, "bot": False})()

    before = await get_balance(BANNED)
    await cog.on_reaction_add(_event_reaction(556, FakeGuild(GUILD_A)), user)
    assert await get_balance(BANNED) == before


async def test_bots_cannot_claim_an_event_coin_drop(bot):
    """The handler never filtered bot reactors either."""
    from src.economy import get_balance

    cog = EconomyCog(bot)
    _state.active_events[557] = {"amount": 10_000, "rewarded": set()}
    other_bot = type("U", (), {"id": 7777, "bot": True})()

    before = await get_balance(7777)
    await cog.on_reaction_add(_event_reaction(557, FakeGuild(GUILD_A)), other_bot)
    assert await get_balance(7777) == before


async def test_unbanned_user_still_claims_an_event_coin_drop(bot):
    """The fix must not break the feature."""
    from src.economy import get_balance

    cog = EconomyCog(bot)
    _ban(GUILD_A, BANNED)
    _state.active_events[558] = {"amount": 10_000, "rewarded": set()}
    user = type("U", (), {"id": CLEAN, "bot": False})()

    before = await get_balance(CLEAN)
    await cog.on_reaction_add(_event_reaction(558, FakeGuild(GUILD_A)), user)
    assert await get_balance(CLEAN) == before + 10_000
