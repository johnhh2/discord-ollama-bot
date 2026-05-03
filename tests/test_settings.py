"""Tier B: !settings subcommands.

Each subcommand mutates state.guild_settings[gid] and persists via
save_guild_settings(). With the `db` fixture these tests round-trip
through real SQL — assertions read both in-memory state AND the
guild_settings DB row.

Representative subset (the remaining ~10 setters all follow the same
shape: parse args → mutate cfg dict → save_guild_settings() → embed):
- shop on/off (toggles dict entry)
- ai-channels (list mutation, accepts mentions or `clear`)
- cmd-blacklist (list mutation read by on_message)
- lottery-channel (sets/clears, plus the new-week reset side effect)
- nsfw on/off (flag toggle)
"""
import json

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.settings_cog import SettingsCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel


pytestmark = pytest.mark.asyncio


def _admin_ctx(guild_id: int = 42, channel_mentions=None) -> FakeCtx:
    """A FakeCtx whose author has guild administrator perms (so the
    server_admin tier on `!settings` lets the call through). Includes
    a `ctx.message.channel_mentions` list for the channel-list setters.
    """
    author = FakeMember(uid=1, administrator=True)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id))
    ctx.message.channel_mentions = list(channel_mentions or [])
    return ctx


async def _read_guild_settings(gid: int) -> dict:
    """Read the persisted guild_settings JSON blob direct from SQLite."""
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT settings_json FROM guild_settings WHERE guild_id=?",
                (gid,),
            )
            row = await cur.fetchone()
    return json.loads(row[0]) if row else {}


# ── !settings shop ────────────────────────────────────────────────────────────

async def test_shop_subcommand_toggles_item_on(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings shop"

    await cog.settings_shop.callback(cog, ctx, "nickname", "off")

    assert _state.guild_settings["42"]["shop_items"]["nickname"] is False
    persisted = await _read_guild_settings(42)
    assert persisted["shop_items"]["nickname"] is False


async def test_shop_subcommand_toggles_item_back_on(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings shop"

    await cog.settings_shop.callback(cog, ctx, "nickname", "off")
    await cog.settings_shop.callback(cog, ctx, "nickname", "on")

    persisted = await _read_guild_settings(42)
    assert persisted["shop_items"]["nickname"] is True


async def test_shop_subcommand_invalid_item_does_not_persist(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings shop"

    await cog.settings_shop.callback(cog, ctx, "notarealitem", "on")

    # No state mutation, no DB write.
    assert _state.guild_settings.get("42", {}).get("shop_items", {}) == {}
    assert await _read_guild_settings(42) == {}


# ── !settings ai-channels ─────────────────────────────────────────────────────

async def test_ai_channels_set_persists_channel_ids(db):
    cog = SettingsCog(bot=None)
    chan_a = FakeTextChannel(ch_id=8001)
    chan_b = FakeTextChannel(ch_id=8002)
    ctx = _admin_ctx(guild_id=42, channel_mentions=[chan_a, chan_b])
    ctx.command.qualified_name = "settings ai-channels"

    await cog.settings_ai_channels.callback(cog, ctx)

    assert _state.guild_settings["42"]["ai_channels"] == [8001, 8002]
    persisted = await _read_guild_settings(42)
    assert persisted["ai_channels"] == [8001, 8002]


async def test_ai_channels_clear_empties_list(db):
    cog = SettingsCog(bot=None)
    # Pre-seed.
    _state.guild_settings["42"] = {"ai_channels": [9001, 9002]}

    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings ai-channels"

    await cog.settings_ai_channels.callback(cog, ctx, "clear")

    assert _state.guild_settings["42"]["ai_channels"] == []
    persisted = await _read_guild_settings(42)
    assert persisted["ai_channels"] == []


async def test_ai_channels_no_args_no_mutation(db):
    """Calling without `clear` and without channel mentions just sends help."""
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings ai-channels"

    await cog.settings_ai_channels.callback(cog, ctx)

    assert "ai_channels" not in _state.guild_settings.get("42", {})
    assert await _read_guild_settings(42) == {}


# ── !settings cmd-blacklist ───────────────────────────────────────────────────

async def test_cmd_blacklist_set_persists(db):
    cog = SettingsCog(bot=None)
    chan = FakeTextChannel(ch_id=7777)
    ctx = _admin_ctx(guild_id=42, channel_mentions=[chan])
    ctx.command.qualified_name = "settings cmd-blacklist"

    await cog.settings_cmd_blacklist.callback(cog, ctx)

    assert _state.guild_settings["42"]["command_blacklist"] == [7777]
    persisted = await _read_guild_settings(42)
    assert persisted["command_blacklist"] == [7777]


async def test_cmd_blacklist_clear_empties(db):
    cog = SettingsCog(bot=None)
    _state.guild_settings["42"] = {"command_blacklist": [9999]}
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings cmd-blacklist"

    await cog.settings_cmd_blacklist.callback(cog, ctx, "clear")

    persisted = await _read_guild_settings(42)
    assert persisted["command_blacklist"] == []


# ── !settings lottery-channel ─────────────────────────────────────────────────

async def test_lottery_channel_set_persists_and_seeds_lottery(db, monkeypatch):
    """Setting the lottery channel for the first time also creates a fresh
    lottery row for the guild (with the seed pool)."""
    # Stub the announce call — it tries to send a Discord embed, not relevant.
    async def _no_announce(*a, **kw):
        return None
    monkeypatch.setattr("src.cogs.settings_cog.announce_new_lottery", _no_announce)

    cog = SettingsCog(bot=None)
    chan = FakeTextChannel(ch_id=5555)
    ctx = _admin_ctx(guild_id=99, channel_mentions=[chan])
    ctx.command.qualified_name = "settings lottery-channel"

    await cog.settings_lottery_channel.callback(cog, ctx)

    assert _state.guild_settings["99"]["lottery_channel"] == 5555
    persisted = await _read_guild_settings(99)
    assert persisted["lottery_channel"] == 5555

    # And a lottery row was seeded for this guild.
    lot = await _persistence.load_lottery(99)
    assert lot["prize_pool"] == 2000


async def test_lottery_channel_clear_disables(db):
    cog = SettingsCog(bot=None)
    _state.guild_settings["42"] = {"lottery_channel": 1234}

    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings lottery-channel"

    await cog.settings_lottery_channel.callback(cog, ctx, "clear")

    assert _state.guild_settings["42"]["lottery_channel"] is None
    persisted = await _read_guild_settings(42)
    assert persisted["lottery_channel"] is None


# ── !settings nsfw ────────────────────────────────────────────────────────────

async def test_nsfw_on_persists_flag(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings nsfw"

    await cog.settings_nsfw.callback(cog, ctx, "on")

    assert _state.guild_settings["42"]["nsfw_enabled"] is True
    persisted = await _read_guild_settings(42)
    assert persisted["nsfw_enabled"] is True


async def test_nsfw_off_persists_flag(db):
    cog = SettingsCog(bot=None)
    _state.guild_settings["42"] = {"nsfw_enabled": True}

    ctx = _admin_ctx(guild_id=42)
    ctx.command.qualified_name = "settings nsfw"

    await cog.settings_nsfw.callback(cog, ctx, "off")

    persisted = await _read_guild_settings(42)
    assert persisted["nsfw_enabled"] is False


# ── permission gating ─────────────────────────────────────────────────────────

async def test_settings_subcommand_blocked_for_non_admin(db):
    """The `settings` tier is server_admin; non-admin callers are denied."""
    cog = SettingsCog(bot=None)
    # Configure the gate explicitly so we exercise the real check.
    _state.command_perms["settings"] = {"tier": "server_admin", "hidden": False}

    author = FakeMember(uid=2, administrator=False)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.message.channel_mentions = []
    ctx.command.qualified_name = "settings ai-channels"

    await cog.settings_ai_channels.callback(cog, ctx, "clear")

    # Denied → no state mutation, no DB write.
    assert "ai_channels" not in _state.guild_settings.get("42", {})
    assert await _read_guild_settings(42) == {}
