"""Admin command log channel — global, bot-admin-only.

Two pieces under test:

1. The `!settings admin-log-channel` setter on SettingsCog. Mutates
   `state.bot_settings["admin_log_channel"]` and persists via
   `save_bot_settings`. With the `db` fixture this round-trips through
   real SQL (the `bot_settings` table).

2. The `_log_admin_command` helper in src/events.py. It's the substantive
   logic: gate by perm tier, gate by configured channel, format the
   embed, and swallow Discord errors so a misconfigured channel never
   breaks command flow.
"""
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.settings_cog import SettingsCog
from src.events import _log_admin_command, _log_command_error

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel, FakeMessage


pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────────────

def _bot_admin_ctx(channel_mentions=None) -> FakeCtx:
    """A FakeCtx whose author is in state.bot_admins (so the bot_admin tier
    on `settings admin-log-channel` lets the call through)."""
    author = FakeMember(uid=1)
    _state.bot_admins.add(author.id)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.message.channel_mentions = list(channel_mentions or [])
    return ctx


async def _read_bot_setting(key: str) -> str | None:
    """Read a single bot_settings row direct from SQLite."""
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT value_text FROM bot_settings WHERE key_name=?",
                (key,),
            )
            row = await cur.fetchone()
    return row[0] if row else None


class _FakeBot:
    """Minimal bot stand-in for `_log_admin_command`. It only calls
    `bot.get_channel(id)` and (if that returns None) `bot.fetch_channel(id)`."""

    def __init__(self, channel=None, fetch_exc: Exception | None = None):
        self._channel = channel
        self._fetch_exc = fetch_exc
        self.get_channel_calls: list[int] = []
        self.fetch_channel_calls: list[int] = []

    def get_channel(self, cid: int):
        self.get_channel_calls.append(cid)
        return self._channel

    async def fetch_channel(self, cid: int):
        self.fetch_channel_calls.append(cid)
        if self._fetch_exc is not None:
            raise self._fetch_exc
        return self._channel


def _ctx_for_command(qualified_name: str) -> FakeCtx:
    """A FakeCtx wired up so `_log_admin_command` can read every field it
    touches: author/guild/channel/message.content + ctx.command."""
    author = FakeMember(uid=999, display_name="alice")
    guild = FakeGuild(gid=42, name="my-guild")
    channel = FakeTextChannel(ch_id=7000, name="general")
    ctx = FakeCtx(author=author, guild=guild, channel=channel)
    ctx.command.qualified_name = qualified_name
    ctx.message.content = f"!{qualified_name} arg1 arg2"
    return ctx


# ── 1. Setter ────────────────────────────────────────────────────────────────

async def test_admin_log_channel_set_persists(db):
    """Setting via channel mention writes the channel id into
    state.bot_settings AND the bot_settings DB table."""
    cog = SettingsCog(bot=None)
    chan = FakeTextChannel(ch_id=5555)
    ctx = _bot_admin_ctx(channel_mentions=[chan])
    ctx.command.qualified_name = "settings admin-log-channel"

    await cog.settings_admin_log_channel.callback(cog, ctx)

    assert _state.bot_settings.get("admin_log_channel") == "5555"
    assert await _read_bot_setting("admin_log_channel") == "5555"


async def test_admin_log_channel_clear_removes_key(db):
    """`clear` removes the in-memory key. (DB row may persist with the
    last value — the bot reads from state.bot_settings, and the read path
    uses .get() so a missing key disables logging.)"""
    cog = SettingsCog(bot=None)
    _state.bot_settings["admin_log_channel"] = "9999"

    ctx = _bot_admin_ctx()
    ctx.command.qualified_name = "settings admin-log-channel"

    await cog.settings_admin_log_channel.callback(cog, ctx, "clear")

    assert "admin_log_channel" not in _state.bot_settings


async def test_admin_log_channel_no_args_no_mutation(db):
    """Calling without `clear` and without a channel mention sends help
    and does not touch state.bot_settings."""
    cog = SettingsCog(bot=None)
    ctx = _bot_admin_ctx()
    ctx.command.qualified_name = "settings admin-log-channel"

    await cog.settings_admin_log_channel.callback(cog, ctx)

    assert "admin_log_channel" not in _state.bot_settings
    assert await _read_bot_setting("admin_log_channel") is None


# ── 2. _log_admin_command helper ─────────────────────────────────────────────

async def test_log_noop_when_unconfigured():
    """No `admin_log_channel` set → no fetch, no send, no raise."""
    _state.bot_settings.pop("admin_log_channel", None)
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    await _log_admin_command(bot, ctx)

    assert log_chan.send.await_count == 0
    assert bot.get_channel_calls == []


async def test_log_noop_for_everyone_tier():
    """`everyone`-tier commands never log, even when channel is configured."""
    _state.bot_settings["admin_log_channel"] = "12345"
    _state.command_perms["flip"] = {"tier": "everyone", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("flip")

    await _log_admin_command(bot, ctx)

    assert log_chan.send.await_count == 0
    assert bot.get_channel_calls == []


async def test_log_noop_for_unknown_command_defaults_to_everyone():
    """A command absent from command_perms defaults to `everyone` and does
    not log. This catches any future regression that flips the default."""
    _state.bot_settings["admin_log_channel"] = "12345"
    _state.command_perms.clear()
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("somecommand")

    await _log_admin_command(bot, ctx)

    assert log_chan.send.await_count == 0


async def test_log_sends_for_bot_admin_tier_success():
    """A bot_admin-tier command on the success path posts a single embed
    to the configured channel."""
    _state.bot_settings["admin_log_channel"] = "12345"
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    await _log_admin_command(bot, ctx)

    assert log_chan.send.await_count == 1
    embed = log_chan.send.call_args.kwargs["embed"]
    assert "Admin Command" in embed.title
    assert "Error" not in embed.title
    # Description carries identifying fields.
    assert "alice" in embed.description
    assert "my-guild" in embed.description
    assert "bot_admin" in embed.description
    assert "!adminhelp arg1 arg2" in embed.description


async def test_log_sends_for_server_admin_tier_success():
    """`server_admin` tier also logs (the hook is bot_admin OR server_admin)."""
    _state.bot_settings["admin_log_channel"] = "12345"
    _state.command_perms["clear"] = {"tier": "server_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("clear")

    await _log_admin_command(bot, ctx)

    assert log_chan.send.await_count == 1
    embed = log_chan.send.call_args.kwargs["embed"]
    assert "server_admin" in embed.description


async def test_error_log_routes_to_bug_report_channel(db):
    """Command errors now auto-file a bug report into `bug_report_channel`
    instead of posting to a separate `error_log_channel`. The embed carries
    the exception details and the seeded ✖️/⚙️/✅/🛑/🔇 reactions enable triage."""
    _state.bot_settings["bug_report_channel"] = "67890"
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    _state.error_mutes.clear()
    posted = FakeMessage(message_id=555)
    posted.channel = FakeTextChannel(ch_id=67890)
    log_chan = FakeTextChannel(ch_id=67890)
    log_chan.send = AsyncMock(return_value=posted)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    err = RuntimeError("kaboom")
    await _log_command_error(bot, ctx, err)

    assert log_chan.send.await_count == 1
    embed = log_chan.send.call_args.kwargs["embed"]
    assert "Error" in embed.title
    assert "RuntimeError" in embed.description
    assert "kaboom" in embed.description
    # All five triage reactions seeded on the posted embed (order: ✖️ ⚙️ ✅ 🛑 🔇).
    reactions = [c.args[0] for c in posted.add_reaction.await_args_list]
    assert reactions == ["✖️", "⚙️", "✅", "🛑", "\U0001F507"]


async def test_error_log_fires_for_everyone_tier(db):
    """Errors on `everyone`-tier commands also file an auto-bug-report — the
    previous admin-only gating silently dropped them."""
    _state.bot_settings["bug_report_channel"] = "67890"
    _state.bot_settings.pop("admin_log_channel", None)
    _state.bot_settings.pop("error_log_channel", None)
    _state.command_perms["bugreport"] = {"tier": "everyone", "hidden": False}
    _state.error_mutes.clear()
    posted = FakeMessage(message_id=556)
    posted.channel = FakeTextChannel(ch_id=67890)
    log_chan = FakeTextChannel(ch_id=67890)
    log_chan.send = AsyncMock(return_value=posted)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("bugreport")

    await _log_command_error(bot, ctx, RuntimeError("oops"))

    assert log_chan.send.await_count == 1


async def test_error_log_no_op_when_unconfigured():
    """No `bug_report_channel` set → no fetch, no send, no raise."""
    _state.bot_settings.pop("bug_report_channel", None)
    _state.bot_settings.pop("error_log_channel", None)
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    await _log_command_error(bot, ctx, RuntimeError("kaboom"))

    assert log_chan.send.await_count == 0
    assert bot.get_channel_calls == []


async def test_error_log_skipped_when_muted(db):
    """If the (command, type, message) key is already in `state.error_mutes`,
    the error path is a silent no-op — no fetch, no send."""
    _state.bot_settings["bug_report_channel"] = "67890"
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=67890)
    log_chan.send = AsyncMock(return_value=FakeMessage(message_id=557))
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    # Pre-seed the mute set with the key we know _build_error_mute_key will compose.
    from src.events import _build_error_mute_key
    err = RuntimeError("kaboom")
    _state.error_mutes.clear()
    _state.error_mutes.add(_build_error_mute_key(ctx, err))

    await _log_command_error(bot, ctx, err)

    assert log_chan.send.await_count == 0
    assert bot.get_channel_calls == []


async def test_log_swallows_send_forbidden():
    """If the configured log channel rejects the send (Forbidden), the
    helper must not raise — admin command flow can't depend on it."""
    _state.bot_settings["admin_log_channel"] = "12345"
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    log_chan.send = AsyncMock(
        side_effect=discord.Forbidden(_FakeResp(403), "no perms")
    )
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    # Must not raise.
    await _log_admin_command(bot, ctx)
    assert log_chan.send.await_count == 1


async def test_log_swallows_unknown_channel():
    """Stale/invalid channel id (NotFound from fetch_channel, get_channel
    returns None) is swallowed and no send is attempted."""
    _state.bot_settings["admin_log_channel"] = "99999999"
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    bot = _FakeBot(
        channel=None,
        fetch_exc=discord.NotFound(_FakeResp(404), "no such channel"),
    )
    ctx = _ctx_for_command("adminhelp")

    await _log_admin_command(bot, ctx)

    # Tried get_channel, fell through to fetch_channel which raised — and
    # the helper swallowed it.
    assert bot.get_channel_calls == [99999999]
    assert bot.fetch_channel_calls == [99999999]


async def test_log_swallows_invalid_channel_id_string():
    """A non-numeric channel id (e.g. corrupted DB row) raises ValueError
    on `int(...)` and must be swallowed."""
    _state.bot_settings["admin_log_channel"] = "not-a-number"
    _state.command_perms["adminhelp"] = {"tier": "bot_admin", "hidden": False}
    log_chan = FakeTextChannel(ch_id=12345)
    bot = _FakeBot(channel=log_chan)
    ctx = _ctx_for_command("adminhelp")

    # Must not raise.
    await _log_admin_command(bot, ctx)

    # No send attempted.
    assert log_chan.send.await_count == 0


# ── tiny helpers ─────────────────────────────────────────────────────────────


class _FakeResp:
    """Minimal stand-in for the aiohttp response object discord.HTTPException
    subclasses require as their first arg."""

    def __init__(self, status: int):
        self.status = status
        self.reason = "test"
