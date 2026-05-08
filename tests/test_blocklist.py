"""Tests for the per-guild and global blocklist (!ban / !unban / !globalban / !globalunban)."""
import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.admin_cog import AdminCog
from src.permissions import is_bannable

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()
        self.process_commands_calls = []

    async def process_commands(self, message):
        self.process_commands_calls.append(message)


def _admin_ctx(uid: int = 1, guild_id: int = 42) -> FakeCtx:
    """Build a FakeCtx whose author is in state.bot_admins (so requires_perm passes)."""
    author = FakeMember(uid=uid)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id))
    ctx.bot = _StubBot()
    _state.bot_admins.add(uid)
    return ctx


# ── is_bannable ──────────────────────────────────────────────────────────────

def test_is_bannable_bot_account_returns_true():
    m = FakeMember(uid=10)
    m.bot = True
    m.guild = FakeGuild(gid=42)
    assert is_bannable(m) is True


def test_is_bannable_bot_admin_returns_false():
    m = FakeMember(uid=10)
    m.guild = FakeGuild(gid=42)
    _state.bot_admins.add(10)
    assert is_bannable(m) is False


def test_is_bannable_discord_administrator_returns_false():
    m = FakeMember(uid=10, administrator=True)
    m.guild = FakeGuild(gid=42)
    assert is_bannable(m) is False


def test_is_bannable_server_admin_override_returns_false():
    m = FakeMember(uid=10)
    m.guild = FakeGuild(gid=42)
    _state.user_perm_overrides[(42, 10)] = "server_admin"
    assert is_bannable(m) is False


def test_is_bannable_bot_admin_override_returns_false():
    m = FakeMember(uid=10)
    m.guild = FakeGuild(gid=42)
    _state.user_perm_overrides[(42, 10)] = "bot_admin"
    assert is_bannable(m) is False


def test_is_bannable_regular_user_returns_true():
    m = FakeMember(uid=10)
    m.guild = FakeGuild(gid=42)
    assert is_bannable(m) is True


# ── persistence round-trip ───────────────────────────────────────────────────

async def _read_block_row(guild_id: int, user_id: int):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT reason, banned_by FROM blocklist WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
            return await cur.fetchone()


async def _read_global_block_row(user_id: int):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT reason, banned_by FROM global_blocklist WHERE user_id=?",
                (user_id,),
            )
            return await cur.fetchone()


@pytest.mark.asyncio
async def test_save_blocklist_upsert_and_delete(db):
    await _persistence.save_blocklist(42, 500, "spammer", banned_by=1)
    row = await _read_block_row(42, 500)
    assert row == ("spammer", 1)

    # Re-save with a new reason should update in place.
    await _persistence.save_blocklist(42, 500, "raider", banned_by=2)
    row = await _read_block_row(42, 500)
    assert row == ("raider", 2)

    await _persistence.delete_blocklist(42, 500)
    assert await _read_block_row(42, 500) is None


@pytest.mark.asyncio
async def test_save_global_blocklist_upsert_and_delete(db):
    await _persistence.save_global_blocklist(500, "evading", banned_by=1)
    assert await _read_global_block_row(500) == ("evading", 1)

    await _persistence.delete_global_blocklist(500)
    assert await _read_global_block_row(500) is None


# ── !ban / !unban command flows ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ban_persists_and_updates_state(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "ban"
    target = FakeMember(uid=500)
    target.guild = ctx.guild

    await cog.cmd_ban.callback(cog, ctx, user=target, reason="spammer")

    assert (42, 500) in _state.blocklist
    assert _state.blocklist[(42, 500)]["reason"] == "spammer"
    assert _state.blocklist[(42, 500)]["banned_by"] == 1
    assert await _read_block_row(42, 500) == ("spammer", 1)


@pytest.mark.asyncio
async def test_ban_rejects_protected_targets(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "ban"

    # Discord administrator
    admin_target = FakeMember(uid=600, administrator=True)
    admin_target.guild = ctx.guild
    await cog.cmd_ban.callback(cog, ctx, user=admin_target, reason="x")
    assert (42, 600) not in _state.blocklist

    # Bot admin (env-driven)
    _state.bot_admins.add(602)
    botadmin_target = FakeMember(uid=602)
    botadmin_target.guild = ctx.guild
    await cog.cmd_ban.callback(cog, ctx, user=botadmin_target, reason="x")
    assert (42, 602) not in _state.blocklist

    # Bot accounts ARE bannable (sanity check the inverse)
    bot_target = FakeMember(uid=601)
    bot_target.bot = True
    bot_target.guild = ctx.guild
    await cog.cmd_ban.callback(cog, ctx, user=bot_target, reason="x")
    assert (42, 601) in _state.blocklist


@pytest.mark.asyncio
async def test_unban_removes_from_state_and_db(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "unban"
    target = FakeMember(uid=500)
    target.guild = ctx.guild

    # Seed a ban first.
    await _persistence.save_blocklist(42, 500, "spammer", banned_by=1)
    _state.blocklist[(42, 500)] = {"reason": "spammer", "banned_by": 1, "banned_at": None}

    await cog.cmd_unban.callback(cog, ctx, user=target)

    assert (42, 500) not in _state.blocklist
    assert await _read_block_row(42, 500) is None


@pytest.mark.asyncio
async def test_unban_user_not_banned_sends_error(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "unban"
    target = FakeMember(uid=500)
    target.guild = ctx.guild

    await cog.cmd_unban.callback(cog, ctx, user=target)

    assert any("Not Banned" in (e.title or "") for e in ctx.sent_embeds)


# ── !globalban / !globalunban ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_globalban_persists_and_updates_state(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "globalban"
    target = FakeMember(uid=500)
    target.guild = ctx.guild

    await cog.cmd_globalban.callback(cog, ctx, user=target, reason="evading")

    assert 500 in _state.global_blocklist
    assert _state.global_blocklist[500]["reason"] == "evading"
    assert await _read_global_block_row(500) == ("evading", 1)


@pytest.mark.asyncio
async def test_globalunban_removes_state_and_db(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "globalunban"
    target = FakeMember(uid=500)
    target.guild = ctx.guild

    await _persistence.save_global_blocklist(500, "evading", banned_by=1)
    _state.global_blocklist[500] = {"reason": "evading", "banned_by": 1, "banned_at": None}

    await cog.cmd_globalunban.callback(cog, ctx, user=target)

    assert 500 not in _state.global_blocklist
    assert await _read_global_block_row(500) is None


@pytest.mark.asyncio
async def test_globalban_rejects_protected_targets(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "globalban"

    _state.bot_admins.add(700)
    target = FakeMember(uid=700)
    target.guild = ctx.guild

    await cog.cmd_globalban.callback(cog, ctx, user=target, reason="x")

    assert 700 not in _state.global_blocklist


# ── on_message gating ────────────────────────────────────────────────────────

class _BannedAuthor:
    """Bare author stand-in for on_message tests."""
    def __init__(self, uid: int):
        self.id = uid
        self.bot = False


def _on_message_with_banned_author(uid: int, guild_id: int | None):
    msg = FakeMessage(content="hi", author=_BannedAuthor(uid=uid))
    msg.guild = FakeGuild(gid=guild_id) if guild_id is not None else None
    return msg


async def _drive_on_message(message) -> int:
    """Run EventsCog.on_message against `message` and return the new value of
    state.stats_messages_seen so the test can confirm the gate fired."""
    from src.events import EventsCog  # cog wraps the listeners

    bot = _StubBot()
    cog = EventsCog(bot)
    before = _state.stats_messages_seen
    # The Cog.listener decorator stashes the function under the same name.
    await cog.on_message(message)
    return _state.stats_messages_seen - before


@pytest.mark.asyncio
async def test_on_message_silent_for_per_guild_blocklist():
    _state.blocklist[(42, 999)] = {"reason": "x", "banned_by": 1, "banned_at": None}
    msg = _on_message_with_banned_author(uid=999, guild_id=42)
    delta = await _drive_on_message(msg)
    assert delta == 0  # gated before stats increment


@pytest.mark.asyncio
async def test_on_message_silent_for_global_blocklist():
    _state.global_blocklist[999] = {"reason": "x", "banned_by": 1, "banned_at": None}
    msg = _on_message_with_banned_author(uid=999, guild_id=42)
    delta = await _drive_on_message(msg)
    assert delta == 0


# ── boot-time hydration ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_init_db_state_hydrates_blocklists(db):
    await _persistence.save_blocklist(42, 500, "x", banned_by=1)
    await _persistence.save_global_blocklist(600, "y", banned_by=2)
    _state.blocklist.clear()
    _state.global_blocklist.clear()

    await _persistence.init_db_state()

    assert (42, 500) in _state.blocklist
    assert _state.blocklist[(42, 500)]["reason"] == "x"
    assert _state.blocklist[(42, 500)]["banned_by"] == 1

    assert 600 in _state.global_blocklist
    assert _state.global_blocklist[600]["reason"] == "y"
