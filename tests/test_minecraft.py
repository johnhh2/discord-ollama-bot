"""Minecraft Bedrock status cog: !mc, the monitor fold, announce, presence.

No network anywhere — fetch_mc_status is monkeypatched at module scope
(src.cogs.minecraft_cog.fetch_mc_status), and the monitor loop is driven by
awaiting its .coro directly instead of starting the tasks.Loop.
"""
import json
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.minecraft_cog as mc_mod
from src.cogs.minecraft_cog import (
    MinecraftCog, MonitorState, McStatus, _mc_events,
)
from src.cogs.settings_cog import SettingsCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel

pytestmark = pytest.mark.asyncio


def _status(players=3, max_players=10, latency=42):
    return McStatus(
        players=players, max_players=max_players, latency_ms=latency,
        version="1.21.51", motd="My Server", gamemode="Survival",
        map_name="world",
    )


class _FakeBot:
    def __init__(self, guilds=(), channels=None):
        self.guilds = list(guilds)
        self._channels = dict(channels or {})
        self.change_presence = AsyncMock()

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise RuntimeError(f"unknown channel {cid}")
        return ch


def _make_cog(monkeypatch, bot=None, host="mc.example.com"):
    """Build the cog without starting the tasks.Loop (host is patched empty
    during __init__), then enable the feature at the given host."""
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", "")
    cog = MinecraftCog(bot)
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", host)
    return cog


def _mc_ctx():
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=42))
    ctx.command.qualified_name = "mc"
    return ctx


# ── _mc_events (pure fold) ────────────────────────────────────────────────────

async def test_first_success_baselines_silently():
    nxt, events = _mc_events(MonitorState(), _status(players=4))
    assert events == []
    assert nxt.online is True and nxt.count == 4 and nxt.fail_streak == 0


async def test_server_down_at_boot_never_alerts():
    st = MonitorState()
    for _ in range(5):
        st, events = _mc_events(st, None)
        assert events == []
    assert st.online is None


async def test_single_failure_is_debounced():
    st = MonitorState(online=True, count=3)
    nxt, events = _mc_events(st, None)
    assert events == []
    assert nxt.online is True and nxt.count == 3 and nxt.fail_streak == 1


async def test_second_consecutive_failure_goes_offline():
    st = MonitorState(online=True, count=3, fail_streak=1)
    nxt, events = _mc_events(st, None)
    assert events == ["went_offline"]
    assert nxt.online is False


async def test_blip_then_recovery_stays_quiet():
    st = MonitorState(online=True, count=3)
    st, _ = _mc_events(st, None)              # one dropped packet
    nxt, events = _mc_events(st, _status(players=3))
    assert events == []
    assert nxt.online is True and nxt.fail_streak == 0


async def test_recovery_after_offline_announces():
    st = MonitorState(online=False, fail_streak=4)
    nxt, events = _mc_events(st, _status(players=2))
    assert events == ["came_online"]
    assert nxt.online is True and nxt.count == 2


async def test_count_changes_emit_events():
    st = MonitorState(online=True, count=2)
    _, up = _mc_events(st, _status(players=4))
    _, down = _mc_events(MonitorState(online=True, count=4), _status(players=1))
    _, same = _mc_events(MonitorState(online=True, count=4), _status(players=4))
    assert up == ["count_up"]
    assert down == ["count_down"]
    assert same == []


# ── !mc command ───────────────────────────────────────────────────────────────

async def test_mc_not_configured(monkeypatch):
    cog = _make_cog(monkeypatch, host="")
    ctx = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx)
    assert "isn't configured" in ctx.sent_embeds[0].description


async def test_mc_online_embed(monkeypatch):
    cog = _make_cog(monkeypatch)

    async def _fake_fetch():
        return _status(players=3, max_players=10, latency=42)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    ctx = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx)

    embed = ctx.sent_embeds[0]
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Players"] == "3/10"
    assert fields["Ping"] == "42 ms"
    assert fields["Version"] == "1.21.51"
    assert fields["MOTD"] == "My Server"
    assert cog._last_seen_online is not None


async def test_mc_offline_embed(monkeypatch):
    cog = _make_cog(monkeypatch)

    async def _fake_fetch():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    ctx = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx)
    assert "Offline" in ctx.sent_embeds[0].title
    assert "didn't respond" in ctx.sent_embeds[0].description


# ── Monitor loop: announce + presence ────────────────────────────────────────

async def _tick(cog):
    await cog.mc_monitor.coro(cog)


async def test_monitor_announces_to_configured_guilds_only(monkeypatch):
    channel = FakeTextChannel(ch_id=555)
    bot = _FakeBot(guilds=[FakeGuild(gid=1), FakeGuild(gid=2)],
                   channels={555: channel})
    _state.guild_settings["1"] = {"minecraft_channel": 555}
    _state.guild_settings["2"] = {}

    cog = _make_cog(monkeypatch, bot=bot)
    cog._monitor = MonitorState(online=False, fail_streak=3)

    async def _fake_fetch():
        return _status(players=2)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)

    channel.send.assert_called_once()
    embed = channel.send.call_args.kwargs["embed"]
    assert "Online" in embed.title
    assert "2/10" in embed.description


async def test_monitor_count_change_message(monkeypatch):
    channel = FakeTextChannel(ch_id=555)
    bot = _FakeBot(guilds=[FakeGuild(gid=1)], channels={555: channel})
    _state.guild_settings["1"] = {"minecraft_channel": 555}

    cog = _make_cog(monkeypatch, bot=bot)
    cog._monitor = MonitorState(online=True, count=2)

    async def _fake_fetch():
        return _status(players=3)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)

    content = channel.send.call_args.kwargs["content"]
    assert "A player joined" in content
    assert "3/10" in content


async def test_monitor_offline_alert_only_after_debounce(monkeypatch):
    channel = FakeTextChannel(ch_id=555)
    bot = _FakeBot(guilds=[FakeGuild(gid=1)], channels={555: channel})
    _state.guild_settings["1"] = {"minecraft_channel": 555}

    cog = _make_cog(monkeypatch, bot=bot)
    cog._monitor = MonitorState(online=True, count=3)

    async def _fake_fetch():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)
    channel.send.assert_not_called()          # first miss: debounced
    await _tick(cog)
    channel.send.assert_called_once()         # second miss: alert
    assert "Offline" in channel.send.call_args.kwargs["embed"].title


async def test_presence_updates_only_on_change(monkeypatch):
    bot = _FakeBot(guilds=[])
    cog = _make_cog(monkeypatch, bot=bot)

    async def _fake_fetch():
        return _status(players=3)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)
    await _tick(cog)

    bot.change_presence.assert_called_once()
    activity = bot.change_presence.call_args.kwargs["activity"]
    assert "3/10" in activity.name


# ── !settings minecraft-channel ───────────────────────────────────────────────

async def _read_guild_settings(gid: int) -> dict:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT settings_json FROM guild_settings WHERE guild_id=?",
                (gid,),
            )
            row = await cur.fetchone()
    return json.loads(row[0]) if row else {}


def _admin_ctx(channel_mentions=None) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(uid=1, administrator=True),
                  guild=FakeGuild(gid=42))
    ctx.command.qualified_name = "settings minecraft-channel"
    ctx.message.channel_mentions = list(channel_mentions or [])
    return ctx


async def test_settings_minecraft_channel_set(db):
    cog = SettingsCog(bot=None)
    channel = FakeTextChannel(ch_id=555)
    ctx = _admin_ctx(channel_mentions=[channel])

    await cog.settings_minecraft_channel.callback(cog, ctx)

    assert _state.guild_settings["42"]["minecraft_channel"] == 555
    persisted = await _read_guild_settings(42)
    assert persisted["minecraft_channel"] == 555


async def test_settings_minecraft_channel_clear(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx(channel_mentions=[FakeTextChannel(ch_id=555)])
    await cog.settings_minecraft_channel.callback(cog, ctx)

    ctx2 = _admin_ctx()
    await cog.settings_minecraft_channel.callback(cog, ctx2, "clear")

    assert _state.guild_settings["42"]["minecraft_channel"] is None
    persisted = await _read_guild_settings(42)
    assert persisted["minecraft_channel"] is None
