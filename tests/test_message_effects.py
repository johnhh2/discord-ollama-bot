"""Tier A: on_message inline-mutation blocks.

These cover the per-message side effects in src/events.py:on_message that
mutate state and (in the tax case) move real coins:

- Tax deduction: taxed user pays SHOP_TAX_PER_MESSAGE per message; master
  receives it; expired entries auto-cleanup; insurance bypasses; the
  channel-scoped tax only fires in its channel.
- Curse decay: remaining counter decrements; entry deleted at zero.
- Mock decay: same shape.
- Ragebait decay: same shape, plus history accumulation.

The on_message handler is invoked directly on a stub EventsCog. We stub
check_ollama_connected and _passive_ragebait so the ragebait branch
runs without Ollama or background tasks.
"""
import time
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.events as _events
from src.events import EventsCog
from src.config import SHOP_TAX_PER_MESSAGE, SHOP_TAX_DURATION_SECS, SHOP_SPELLCHECK_DURATION_SECS

from tests.fakes.discord import FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()
        self.cogs = {}

    async def fetch_user(self, uid):
        # Used by tax flow when fetch_member fails. Return a stub with
        # display_name so the message formatting can complete.
        return type("U", (), {"display_name": f"user_{uid}", "id": uid})()

    async def process_commands(self, message):
        # on_message tail-calls this to dispatch !commands. We only care
        # about the side-effect blocks before this point.
        return None


class _Channel:
    """on_message channel stand-in with async send and a real id."""
    def __init__(self, ch_id: int = 100):
        self.id = ch_id
        self.send = AsyncMock()


class _Msg:
    """Minimal stand-in for discord.Message that on_message touches.

    on_message walks past the Tier A blocks (tax/curse/mock/ragebait) into
    the auto-daily-reward branch which reads .mentions and other attrs;
    we provide the bare minimum so that branch is a no-op.
    """
    def __init__(self, author, content, guild, channel):
        self.author = author
        self.content = content
        self.guild = guild
        self.channel = channel
        self.mentions = []
        self.reference = None


@pytest.fixture
def bot():
    return _StubBot()


@pytest.fixture
def cog(bot):
    return EventsCog(bot)


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    """Stub the side-effect-y collaborators called from on_message:
    - check_ollama_connected → True so the ragebait branch enters
    - _passive_ragebait → no-op (the AI streaming path is integration-only)
    - _grant_xp → no-op (covered by test_schedulers.py)
    """
    async def _ollama_up():
        return True
    monkeypatch.setattr(_events, "check_ollama_connected", _ollama_up)

    async def _no_rage(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_passive_ragebait", _no_rage)

    async def _no_xp(*a, **kw):
        return (0, False)
    monkeypatch.setattr(_events, "_grant_xp", _no_xp)


async def _read_db_balance(uid: int) -> int | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT balance FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else None


# ── Tax deduction ─────────────────────────────────────────────────────────────

async def test_tax_deducts_per_message_and_credits_master(db, cog):
    payer = FakeMember(uid=1001, display_name="payer")
    master = FakeMember(uid=2001, display_name="master")
    guild = FakeGuild(gid=42)
    guild.members = [payer, master]
    channel = _Channel(ch_id=500)

    await _economy.add_balance(payer.id, 5000)
    _state.active_taxes[payer.id] = {
        "master": master.id,
        "type": "tax",
        "emoji": "💰",
        "channel_id": None,  # all channels
        "activated_at": time.time(),
    }

    msg = _Msg(payer, "hello world", guild, channel)
    await cog.on_message(msg)

    assert await _economy.get_balance(payer.id) == 5000 - SHOP_TAX_PER_MESSAGE
    assert await _economy.get_balance(master.id) == SHOP_TAX_PER_MESSAGE
    assert await _read_db_balance(payer.id) == 5000 - SHOP_TAX_PER_MESSAGE
    # Tax-paid notification posted in the channel.
    channel.send.assert_awaited()
    sent_text = channel.send.call_args.args[0]
    assert "tax" in sent_text.lower() or "💰" in sent_text


async def test_tax_does_not_fire_on_command_messages(db, cog):
    """Lines 385: `not message.content.startswith('!')` — taxed users'
    `!commands` are exempt. Pre-claim auto-daily so we can isolate the
    tax-deduction effect."""
    payer = FakeMember(uid=1002)
    master = FakeMember(uid=2002)
    guild = FakeGuild(gid=42)
    channel = _Channel()

    await _economy.add_balance(payer.id, 5000)
    # Mark today's auto-daily as already claimed.
    _state.economy["users"][str(payer.id)]["daily_date"] = _economy._ct_today()

    _state.active_taxes[payer.id] = {
        "master": master.id, "type": "tax", "emoji": "💰",
        "channel_id": None, "activated_at": time.time(),
    }

    msg = _Msg(payer, "!balance", guild, channel)
    await cog.on_message(msg)

    assert await _economy.get_balance(payer.id) == 5000  # untouched
    assert await _economy.get_balance(master.id) == 0


async def test_tax_skips_when_target_is_insured(db, cog):
    payer = FakeMember(uid=1003)
    master = FakeMember(uid=2003)
    guild = FakeGuild(gid=42)
    guild.members = [payer, master]
    channel = _Channel()

    await _economy.add_balance(payer.id, 5000)
    _state.insurance[str(payer.id)] = {
        "expires_at": time.time() + 3600,
        "protected_from": ["tax"],
    }
    _state.active_taxes[payer.id] = {
        "master": master.id, "type": "tax", "emoji": "💰",
        "channel_id": None, "activated_at": time.time(),
    }

    msg = _Msg(payer, "hello", guild, channel)
    await cog.on_message(msg)

    assert await _economy.get_balance(payer.id) == 5000
    assert await _economy.get_balance(master.id) == 0


async def test_tax_auto_expires_after_duration(db, cog):
    """When `time.time() - activated_at > SHOP_TAX_DURATION_SECS`, the
    tax entry is deleted from state (events.py:389-391)."""
    payer = FakeMember(uid=1004)
    master = FakeMember(uid=2004)
    guild = FakeGuild(gid=42)
    guild.members = [payer, master]
    channel = _Channel()

    await _economy.add_balance(payer.id, 5000)
    # Activated 2× the duration ago — already expired.
    _state.active_taxes[payer.id] = {
        "master": master.id, "type": "tax", "emoji": "💰",
        "channel_id": None,
        "activated_at": time.time() - SHOP_TAX_DURATION_SECS * 2,
    }

    msg = _Msg(payer, "hello", guild, channel)
    await cog.on_message(msg)

    assert payer.id not in _state.active_taxes
    # No deduction either.
    assert await _economy.get_balance(payer.id) == 5000


async def test_tax_channel_scoped_only_fires_in_target_channel(db, cog):
    """When `channel_id` is set, the tax only deducts in that channel."""
    payer = FakeMember(uid=1005)
    master = FakeMember(uid=2005)
    guild = FakeGuild(gid=42)
    guild.members = [payer, master]  # so fetch_member resolves master
    target_channel = _Channel(ch_id=600)
    other_channel = _Channel(ch_id=601)

    await _economy.add_balance(payer.id, 5000)
    _state.active_taxes[payer.id] = {
        "master": master.id, "type": "tax", "emoji": "💰",
        "channel_id": 600, "activated_at": time.time(),
    }

    # Message in OTHER channel: no deduction.
    await cog.on_message(_Msg(payer, "hello", guild, other_channel))
    assert await _economy.get_balance(payer.id) == 5000

    # Message in target channel: deduction.
    await cog.on_message(_Msg(payer, "hello", guild, target_channel))
    assert await _economy.get_balance(payer.id) == 5000 - SHOP_TAX_PER_MESSAGE


# ── Curse decay ───────────────────────────────────────────────────────────────

async def test_curse_decrements_and_replays_in_curse_font(db, cog):
    target = FakeMember(uid=3001)
    guild = FakeGuild(gid=42)
    channel = _Channel()

    _state.active_curses[target.id] = {"cursed_by": 9, "remaining": 3}
    msg = _Msg(target, "the quick brown fox", guild, channel)
    await cog.on_message(msg)

    # Counter decremented but entry still present.
    assert _state.active_curses[target.id]["remaining"] == 2
    # Channel got a cursed-font replay.
    channel.send.assert_awaited_once()


async def test_curse_deletes_entry_when_remaining_hits_zero(db, cog):
    target = FakeMember(uid=3002)
    guild = FakeGuild(gid=42)
    channel = _Channel()

    _state.active_curses[target.id] = {"cursed_by": 9, "remaining": 1}
    await cog.on_message(_Msg(target, "last cursed message", guild, channel))

    assert target.id not in _state.active_curses
    # And it was persisted: the shop_effects row should be gone.
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM shop_effects WHERE user_id=? AND effect_type='curse'",
                (target.id,),
            )
            row = await cur.fetchone()
    assert row is None


# ── Mock decay ────────────────────────────────────────────────────────────────

async def test_mock_decrements_and_replays_in_mocking_font(db, cog):
    target = FakeMember(uid=3003)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=700)

    _state.active_mocks[target.id] = {
        "remaining": 2, "started_by": 9, "channel_id": 700,
    }
    await cog.on_message(_Msg(target, "MoCkInG", guild, channel))

    assert _state.active_mocks[target.id]["remaining"] == 1
    channel.send.assert_awaited_once()


async def test_mock_deletes_entry_at_zero_and_replaces_db_row(db, cog):
    target = FakeMember(uid=3004)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=701)

    _state.active_mocks[target.id] = {
        "remaining": 1, "started_by": 9, "channel_id": 701,
    }
    await cog.on_message(_Msg(target, "last", guild, channel))

    assert target.id not in _state.active_mocks
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM shop_effects WHERE user_id=? AND effect_type='mock'",
                (target.id,),
            )
            row = await cur.fetchone()
    assert row is None


async def test_mock_does_not_fire_in_other_channel(db, cog):
    target = FakeMember(uid=3005)
    guild = FakeGuild(gid=42)
    other_channel = _Channel(ch_id=703)

    _state.active_mocks[target.id] = {
        "remaining": 5, "started_by": 9, "channel_id": 702,
    }
    await cog.on_message(_Msg(target, "msg", guild, other_channel))

    # Counter unchanged because we weren't in the target channel.
    assert _state.active_mocks[target.id]["remaining"] == 5


# ── Ragebait decay ────────────────────────────────────────────────────────────

async def test_ragebait_decrements_and_appends_history(db, cog):
    target = FakeMember(uid=4001, display_name="target")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    _state.active_ragebaits[target.id] = {
        "remaining": 3, "started_by": 9, "history": [], "channel_id": None,
    }
    await cog.on_message(_Msg(target, "what a take", guild, channel))

    rage = _state.active_ragebaits[target.id]
    assert rage["remaining"] == 2
    assert len(rage["history"]) == 1
    assert "target" in rage["history"][0]
    assert "what a take" in rage["history"][0]


async def test_ragebait_deletes_at_zero(db, cog):
    target = FakeMember(uid=4002)
    guild = FakeGuild(gid=42)
    channel = _Channel()

    _state.active_ragebaits[target.id] = {
        "remaining": 1, "started_by": 9, "history": [], "channel_id": None,
    }
    await cog.on_message(_Msg(target, "last one", guild, channel))

    assert target.id not in _state.active_ragebaits
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM shop_effects WHERE user_id=? AND effect_type='ragebait'",
                (target.id,),
            )
            row = await cur.fetchone()
    assert row is None


# ── Insurance protects runtime handlers ───────────────────────────────────────
#
# Insurance is supposed to no-op mock and ragebait at runtime (matching
# the tax handler's behavior). Before this was added, mock/ragebait would
# fire even on insured users — the effect remained in state.active_*
# until its counter ticked down, oblivious to insurance.

async def test_mock_skips_when_target_is_insured(db, cog):
    target = FakeMember(uid=3010)
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=710)

    _state.insurance[str(target.id)] = {
        "expires_at": time.time() + 3600,
        "protected_from": ["mock"],
    }
    _state.active_mocks[target.id] = {
        "remaining": 3, "started_by": 9, "channel_id": 710,
    }
    await cog.on_message(_Msg(target, "hello", guild, channel))

    # Counter must not decrement and no mocking reply sent.
    assert _state.active_mocks[target.id]["remaining"] == 3
    channel.send.assert_not_awaited()


async def test_ragebait_skips_when_target_is_insured(db, cog):
    target = FakeMember(uid=4010, display_name="target")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    _state.insurance[str(target.id)] = {
        "expires_at": time.time() + 3600,
        "protected_from": ["ragebait"],
    }
    _state.active_ragebaits[target.id] = {
        "remaining": 3, "started_by": 9, "history": [], "channel_id": None,
    }
    await cog.on_message(_Msg(target, "spicy take", guild, channel))

    rage = _state.active_ragebaits[target.id]
    # Counter and history must be untouched.
    assert rage["remaining"] == 3
    assert rage["history"] == []


# ── Spellcheck correction ──────────────────────────────────────────────────────

async def test_spellcheck_corrects_message_with_errors(db, cog, monkeypatch):
    """A spellchecked user's message with errors gets an AI-corrected reply
    suffixed with ' *'."""
    target = FakeMember(uid=5001, display_name="typo")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    async def _correct(messages, model=None):
        return "I went to the store."
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "i goed to teh stor", guild, channel))

    channel.send.assert_awaited()
    assert channel.send.call_args.args[0] == "I went to the store. *"


async def test_spellcheck_silent_when_ai_says_correct(db, cog, monkeypatch):
    target = FakeMember(uid=5002, display_name="clean")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    async def _correct(messages, model=None):
        return "CORRECT"
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "This sentence is fine.", guild, channel))

    channel.send.assert_not_awaited()


async def test_spellcheck_silent_when_unchanged(db, cog, monkeypatch):
    target = FakeMember(uid=5003, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    async def _echo(messages, model=None):
        return "same text"
    monkeypatch.setattr(_events, "ollama_complete", _echo)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "same text", guild, channel))

    channel.send.assert_not_awaited()


async def test_spellcheck_does_not_fire_on_command_messages(db, cog, monkeypatch):
    target = FakeMember(uid=5004, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    called = False

    async def _correct(messages, model=None):
        nonlocal called
        called = True
        return "fixed"
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "!balance", guild, channel))

    assert called is False


async def test_spellcheck_expires_and_cleans_up(db, cog, monkeypatch):
    target = FakeMember(uid=5005, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    async def _correct(messages, model=None):
        return "fixed"
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    # Activated 2 days ago, only 1 day purchased → expired.
    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None,
        "activated_at": time.time() - 2 * SHOP_SPELLCHECK_DURATION_SECS,
    }
    await cog.on_message(_Msg(target, "i has errors", guild, channel))

    assert target.id not in _state.active_spellchecks
    channel.send.assert_not_awaited()


async def test_spellcheck_skips_blacklisted_channel(db, cog, monkeypatch):
    target = FakeMember(uid=5006, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=777)
    _state.guild_settings["42"] = {"command_blacklist": [777]}

    async def _correct(messages, model=None):
        return "fixed"
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "i has errors", guild, channel))

    channel.send.assert_not_awaited()


async def test_spellcheck_skips_channel_not_in_whitelist(db, cog, monkeypatch):
    target = FakeMember(uid=5007, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=200)
    _state.guild_settings["42"] = {"command_whitelist": [100]}  # 200 not allowed

    async def _correct(messages, model=None):
        return "fixed"
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "i has errors", guild, channel))

    channel.send.assert_not_awaited()


async def test_spellcheck_fires_in_whitelisted_channel(db, cog, monkeypatch):
    target = FakeMember(uid=5008, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel(ch_id=100)
    _state.guild_settings["42"] = {"command_whitelist": [100]}

    async def _correct(messages, model=None):
        return "I have errors."
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "i has errors", guild, channel))

    channel.send.assert_awaited()
    assert channel.send.call_args.args[0] == "I have errors. *"


async def test_spellcheck_skips_insured_target(db, cog, monkeypatch):
    target = FakeMember(uid=5009, display_name="x")
    guild = FakeGuild(gid=42)
    channel = _Channel()

    async def _correct(messages, model=None):
        return "fixed"
    monkeypatch.setattr(_events, "ollama_complete", _correct)

    _state.insurance[str(target.id)] = {
        "expires_at": time.time() + 3600,
        "protected_from": ["spellcheck"],
    }
    _state.active_spellchecks[target.id] = {
        "started_by": 9, "days": 1, "channel_id": None, "activated_at": time.time(),
    }
    await cog.on_message(_Msg(target, "i has errors", guild, channel))

    channel.send.assert_not_awaited()
