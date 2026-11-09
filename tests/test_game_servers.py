"""V Rising / Vintage Story server notices (src/cogs/game_servers_cog.py)."""
import asyncio
import re
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

import src.state as _state
import src.cogs.game_servers_cog as gs_mod
import src.cogs.minecraft_cog as mc_mod
import src.server_addresses as addr_mod
from src.cogs.game_servers_cog import GameServer, GameServersCog, ServerStatus, _parse_a2s_info
from src.cogs.settings_cog import SettingsCog
from src.guild_config import get_guild_cfg
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel


def _a2s_reply(players=2, max_players=20, version="1.1.4.0") -> bytes:
    strings = b"My Castle\0Vardoran\0VRising\0V Rising\0"
    body = (b"\x11" + strings + struct.pack("<H", 0) + bytes([players, max_players, 0])
            + b"dwl\x00" + version.encode() + b"\0" + b"\x80" + struct.pack("<H", 9876))
    return b"\xff\xff\xff\xffI" + body


class _FakeBot:
    def __init__(self, guilds=(), channels=None):
        self.guilds = list(guilds)
        self._channels = dict(channels or {})

    async def wait_until_ready(self):
        pass

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        return self._channels[cid]


def _scripted(results):
    """A probe returning each result in turn (None = the server didn't answer)."""
    it = iter(results)

    async def _probe(host, port):
        result = next(it)
        if result is None:
            raise OSError("no reply")
        return result
    return _probe


def _cog(monkeypatch, bot, *servers):
    monkeypatch.setattr(gs_mod, "configured_servers", lambda: [])
    monkeypatch.setattr(_state, "bot_settings", {})
    cog = GameServersCog(bot)
    cog.servers = list(servers)
    return cog


def _vrising(results):
    return GameServer("vrising", "V Rising", "vr.example.com", 9877, _scripted(results),
                      joins_key="vrising_join_notices")


def _vintage(n):
    return GameServer("vintagestory", "Vintage Story", "vs.example.com", 42420,
                      _scripted([ServerStatus(0, 0, "")] * n), joins_key=None)


async def _ticks(cog, n):
    for _ in range(n):
        await cog.game_monitor.coro(cog)


def _titles(channel):
    return [c.kwargs["embed"].title for c in channel.send.call_args_list]


# ── A2S ──────────────────────────────────────────────────────────────────────

def test_parse_a2s_info_reads_players_and_version():
    assert _parse_a2s_info(_a2s_reply(players=3, max_players=40, version="1.1.4.0")) == ServerStatus(3, 40, "1.1.4.0")


def test_parse_a2s_info_rejects_other_replies():
    with pytest.raises(ValueError):
        _parse_a2s_info(b"\xff\xff\xff\xffm...")


async def test_a2s_status_answers_the_challenge():
    seen = []

    class _Server(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            seen.append(data)
            if data == gs_mod._A2S_INFO:
                self.transport.sendto(b"\xff\xff\xff\xffA\x01\x02\x03\x04", addr)
            else:
                self.transport.sendto(_a2s_reply(players=5), addr)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_Server, local_addr=("127.0.0.1", 0))
    try:
        port = transport.get_extra_info("sockname")[1]
        status = await gs_mod.a2s_status("127.0.0.1", port)
    finally:
        transport.close()
    assert status.players == 5
    assert seen[1] == gs_mod._A2S_INFO + b"\x01\x02\x03\x04"


async def test_tcp_status_is_online_only_while_the_port_accepts():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert (await gs_mod.tcp_status("127.0.0.1", port)).players == 0
    server.close()
    await server.wait_closed()
    with pytest.raises(OSError):
        await gs_mod.tcp_status("127.0.0.1", port)


def test_vrising_rcon_port_swaps_a2s_for_a_tcp_check(monkeypatch):
    monkeypatch.setattr(gs_mod, "VRISING_SERVER_HOST", "192.168.1.5")
    monkeypatch.setattr(gs_mod, "VINTAGESTORY_SERVER_HOST", "")
    (server,) = gs_mod.configured_servers()
    assert (server.port, server.probe, server.joins_key) == (9877, gs_mod.a2s_status, "vrising_join_notices")

    monkeypatch.setattr(gs_mod, "VRISING_RCON_PORT", 25575)
    (server,) = gs_mod.configured_servers()
    assert (server.port, server.probe, server.joins_key) == (25575, gs_mod.tcp_status, None)


def test_vrising_joins_setting_hidden_when_polled_over_rcon(monkeypatch):
    import src.settings_hub as hub
    keys = lambda: [item.key for item in hub._games({})]  # noqa: E731
    assert "vrising-joins" in keys()
    monkeypatch.setattr(hub, "VRISING_RCON_PORT", 25575)
    assert "vrising-joins" not in keys()


# ── Monitor ──────────────────────────────────────────────────────────────────

async def test_vrising_join_notices_follow_each_guilds_switch(monkeypatch):
    quiet, loud = FakeTextChannel(ch_id=1), FakeTextChannel(ch_id=2)
    bot = _FakeBot([FakeGuild(gid=10), FakeGuild(gid=20)], {1: quiet, 2: loud})
    _state.guild_settings["10"] = {"game_servers_channel": 1, "vrising_join_notices": False}
    _state.guild_settings["20"] = {"game_servers_channel": 2}
    cog = _cog(monkeypatch, bot, _vrising([
        ServerStatus(0, 20, "1.1"), ServerStatus(2, 20, "1.1"), ServerStatus(0, 20, "1.1"), None, None]))

    await _ticks(cog, 5)

    assert _titles(quiet) == ["🔴 V Rising Server Offline"]
    assert _titles(loud) == ["🟢 Player Joined", "🔴 Player Left", "🔴 V Rising Server Offline"]
    joined, left = (c.kwargs["embed"].description for c in loud.send.call_args_list[:2])
    assert joined == "2 players joined the V Rising server — **2/20** online"
    assert left == "2 players left the V Rising server — it's empty now"


async def test_vrising_update_survives_a_bot_restart(monkeypatch):
    channel = FakeTextChannel(ch_id=1)
    bot = _FakeBot([FakeGuild(gid=10)], {1: channel})
    _state.guild_settings["10"] = {"game_servers_channel": 1}
    cog = _cog(monkeypatch, bot, _vrising([ServerStatus(0, 20, "1.1")]))
    await cog._before_monitor()
    await _ticks(cog, 1)
    assert channel.send.call_count == 0
    assert _state.bot_settings["vrising_version"] == "1.1"

    restarted = GameServersCog(bot)
    restarted.servers = [_vrising([ServerStatus(0, 20, "1.2")])]
    await restarted._before_monitor()
    await _ticks(restarted, 1)
    assert channel.send.call_args.kwargs["embed"].description == (
        "The V Rising server was updated: **1.1** → **1.2**")


async def test_vintagestory_posts_status_and_finds_its_version_on_the_master_list(monkeypatch):
    channel = FakeTextChannel(ch_id=1)
    bot = _FakeBot([FakeGuild(gid=10)], {1: channel})
    _state.guild_settings["10"] = {"game_servers_channel": 1}
    server = GameServer("vintagestory", "Vintage Story", "vs.example.com", 42420,
                        _scripted([ServerStatus(0, 0, ""), None, None, ServerStatus(0, 0, ""),
                                   ServerStatus(0, 0, "")]), joins_key=None)
    cog = _cog(monkeypatch, bot, server)
    _state.bot_settings["vintagestory_version"] = "1.20.11"
    await cog._before_monitor()
    listed = iter(["1.20.11", "1.21.0"])
    lookups = []

    async def _listed(host, port):
        lookups.append((host, port))
        return next(listed)
    monkeypatch.setattr(gs_mod, "vintagestory_listed_version", _listed)
    clock = [1000.0]
    monkeypatch.setattr(gs_mod.time, "time", lambda: clock[0])

    await _ticks(cog, 3)            # baseline (same version), then down
    clock[0] += 10
    await _ticks(cog, 1)            # back up: fresh stretch looks the version up again
    clock[0] += 10
    await _ticks(cog, 1)            # found already: no further lookup

    assert lookups == [("vs.example.com", 42420)] * 2
    assert _titles(channel) == ["🔴 Vintage Story Server Offline", "⬆️ Vintage Story Server Updated"]
    assert channel.send.call_args.kwargs["embed"].description == (
        "The Vintage Story server is back up, updated: **1.20.11** → **1.21.0**")


async def test_vintagestory_master_list_lookups_are_spaced_and_capped(monkeypatch):
    polls = gs_mod.VS_LOOKUP_TRIES * 3
    cog = _cog(monkeypatch, _FakeBot(), _vintage(polls))
    calls = []

    async def _unlisted(host, port):
        calls.append(1)
        return None
    monkeypatch.setattr(gs_mod, "vintagestory_listed_version", _unlisted)
    clock = [1000.0]
    monkeypatch.setattr(gs_mod.time, "time", lambda: clock[0])

    for _ in range(polls):
        await _ticks(cog, 1)
        clock[0] += gs_mod.VS_LOOKUP_INTERVAL_SECS / 2

    assert len(calls) == gs_mod.VS_LOOKUP_TRIES


async def test_vintagestory_listed_version_matches_host_and_port(monkeypatch):
    listing = {"data": [{"serverIP": "203.0.113.5:42421", "gameVersion": "9"},
                        {"serverIP": "203.0.113.5:42420", "gameVersion": "1.21.0"}]}

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        async def json(self, content_type=None):
            return listing

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, timeout=None):
            return _Resp()

    async def _resolve(host, port, type=0):
        return [(None, None, None, "", ("203.0.113.5", port))]
    monkeypatch.setattr(gs_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _resolve)

    assert await gs_mod.vintagestory_listed_version("vs.example.com", 42420) == "1.21.0"
    assert await gs_mod.vintagestory_listed_version("vs.example.com", 1) is None


# ── !gameservers ─────────────────────────────────────────────────────────────

def _ctx(name="gameservers"):
    return FakeCtx(author=FakeMember(1), guild=FakeGuild(gid=42), command_name=name)


async def test_gameservers_lists_every_configured_server(monkeypatch):
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", "mc.example.com")

    async def _mc():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _mc)
    cog = _cog(monkeypatch, _FakeBot(), _vrising([ServerStatus(3, 20, "1.1")]), _vintage(1))
    _state.bot_settings["vintagestory_version"] = "1.21.0"
    ctx = _ctx()

    await cog.cmd_gameservers.callback(cog, ctx)

    assert ctx.sent_embeds[-1].description.split("\n") == [
        "🔴 **Minecraft** — offline",
        "🟢 **V Rising** — 3/20 online · 1.1",
        "🟢 **Vintage Story** — online · 1.21.0",
        "",
        "Details: `!mc`, `!vrising`, `!vintagestory`",
    ]


async def test_gameservers_with_nothing_configured(monkeypatch):
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", "")
    cog = _cog(monkeypatch, _FakeBot())
    ctx = _ctx()
    await cog.cmd_gameservers.callback(cog, ctx)
    assert ctx.sent_embeds[-1].description == "No game servers are configured."


async def test_vrising_status_card(monkeypatch):
    cog = _cog(monkeypatch, _FakeBot(), _vrising([ServerStatus(2, 20, "1.1"), ServerStatus(2, 20, "1.1")]))
    await _ticks(cog, 1)
    ctx = _ctx("vrising")

    await cog.cmd_vrising.callback(cog, ctx)

    embed = ctx.sent_embeds[-1]
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "🟢 V Rising Server Status"
    assert fields["Players"] == "2/20" and fields["Version"] == "1.1"
    assert fields["Online since"].startswith("<t:")


async def test_vintagestory_offline_and_unconfigured(monkeypatch):
    cog = _cog(monkeypatch, _FakeBot(), GameServer(
        "vintagestory", "Vintage Story", "vs.example.com", 42420, _scripted([None]), joins_key=None))
    cog._last_seen_online["vintagestory"] = 1_700_000_000
    ctx = _ctx("vintagestory")
    await cog.cmd_vintagestory.callback(cog, ctx)
    assert ctx.sent_embeds[-1].title == "🔴 Vintage Story Server Offline"
    assert "<t:1700000000:R>" in ctx.sent_embeds[-1].description

    await cog.cmd_vrising.callback(cog, ctx)
    assert "set `VRISING_SERVER_HOST`" in ctx.sent_embeds[-1].description


async def test_unknown_version_is_left_off_the_card(monkeypatch):
    _state.bot_settings.pop("vintagestory_version", None)
    cog = _cog(monkeypatch, _FakeBot(), _vintage(1))
    ctx = _ctx("vintagestory")
    await cog.cmd_vintagestory.callback(cog, ctx)
    assert ctx.sent_embeds[-1].title == "🟢 Vintage Story Server Status"
    assert "Version" not in [f.name for f in ctx.sent_embeds[-1].fields]


async def test_gameservers_minecraft_runs_mc(monkeypatch):
    ran = []

    class _Mc:
        @commands.command(name="mc")
        async def cmd_mc(self, ctx):
            ran.append(ctx.invoked_with)

    bot = _FakeBot()
    bot.get_cog = lambda name: _Mc() if name == "MinecraftCog" else None
    cog = _cog(monkeypatch, bot)
    await cog.cmd_minecraft.callback(cog, _ctx("gameservers minecraft"))
    assert ran == ["mc"]


async def test_names_reach_the_commands(monkeypatch):
    monkeypatch.setattr(gs_mod, "configured_servers", lambda: [])
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    cog = GameServersCog(bot)
    await bot.add_cog(cog)
    for name in ("gameservers", "gameserver", "servers", "server"):
        assert bot.get_command(name) is cog.cmd_gameservers
    assert bot.get_command("servers vrising").callback is cog.cmd_vrising.callback
    assert bot.get_command("vrising").callback is cog.cmd_vrising.callback
    assert bot.get_command("vintage").callback is cog.cmd_vintagestory.callback
    assert bot.get_command("server mc").callback is cog.cmd_minecraft.callback
    await bot.remove_cog("GameServersCog")
    assert bot.get_command("vrising") is None


# ── Settings ─────────────────────────────────────────────────────────────────

def _admin_ctx(name, channel_mentions=()):
    ctx = FakeCtx(author=FakeMember(1, administrator=True), guild=FakeGuild(gid=42), command_name=name)
    ctx.message.channel_mentions = list(channel_mentions)
    return ctx


async def test_old_minecraft_channel_spellings_set_the_game_servers_channel(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx("settings channel minecraft", [FakeTextChannel(ch_id=555)])
    await cog.settings_minecraft_channel_legacy_sub.callback(cog, ctx)
    assert _state.guild_settings["42"]["game_servers_channel"] == 555
    ctx = _admin_ctx("settings minecraft-channel")
    await cog.settings_minecraft_channel_legacy.callback(cog, ctx, "clear")
    assert _state.guild_settings["42"]["game_servers_channel"] is None


async def test_settings_vrising_joins_toggles(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx("settings vrising-joins")
    await cog.settings_vrising_joins.callback(cog, ctx, "off")
    assert _state.guild_settings["42"]["vrising_join_notices"] is False
    await cog.settings_vrising_joins.callback(cog, ctx, "maybe")
    assert "Usage:" in ctx.sent_embeds[-1].description


# ── Show IP address ──────────────────────────────────────────────────────────

@pytest.fixture
def _hosts(monkeypatch):
    monkeypatch.setattr(addr_mod, "MC_SERVER_HOST", "mc.example.com")
    monkeypatch.setattr(addr_mod, "MC_SERVER_PORT", 19132)
    monkeypatch.setattr(addr_mod, "VRISING_SERVER_HOST", "vr.example.com")
    monkeypatch.setattr(addr_mod, "VINTAGESTORY_SERVER_HOST", "vs.example.com")
    monkeypatch.setattr(addr_mod, "VINTAGESTORY_SERVER_PORT", 42420)

    async def _ip(host):
        return "203.0.113.7"
    monkeypatch.setattr(addr_mod, "_public_ip", _ip)


def _set_level(uid, level, gid=42):
    _state.leveling.setdefault(str(gid), {})[str(uid)] = {"level": level - 1}


def _ip_level(level, gid=42):
    get_guild_cfg(gid)[addr_mod.MIN_LEVEL_KEY] = level


def _click(view, uid):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    interaction = SimpleNamespace(user=FakeMember(uid), guild=FakeGuild(gid=42), response=response,
                                  followup=SimpleNamespace(send=AsyncMock()))
    return view.children[0].callback(interaction), interaction


def _private_text(interaction):
    for mock in (interaction.response.send_message, interaction.followup.send):
        if mock.await_count:
            assert mock.call_args.kwargs["ephemeral"] is True
            return mock.call_args.args[0]


async def test_no_ip_button_until_a_level_is_set(monkeypatch, _hosts):
    cog = _cog(monkeypatch, _FakeBot(), _vintage(1))
    _set_level(1, 99)
    ctx = _ctx("vintagestory")
    await cog.cmd_vintagestory.callback(cog, ctx)
    assert ctx.sent_views == []


async def test_ip_button_shows_only_for_members_at_the_level(monkeypatch, _hosts):
    cog = _cog(monkeypatch, _FakeBot(), _vintage(2))
    _ip_level(10)
    _set_level(1, 9)
    ctx = _ctx("vintagestory")
    await cog.cmd_vintagestory.callback(cog, ctx)
    assert ctx.sent_views == []

    _set_level(1, 10)
    await cog.cmd_vintagestory.callback(cog, ctx)
    button = ctx.sent_views[-1].children[0]
    assert button.item.label == "Show IP address" and button.custom_id == "srvaddr:vintagestory"


async def test_gameservers_button_reveals_all_three_privately(monkeypatch, _hosts):
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", "")
    cog = _cog(monkeypatch, _FakeBot(), _vintage(1))
    _ip_level(5)
    _set_level(1, 5)
    ctx = _ctx()
    await cog.cmd_gameservers.callback(cog, ctx)

    click, interaction = _click(ctx.sent_views[-1], uid=1)
    await click
    assert _private_text(interaction).split("\n") == [
        "⛏️ Minecraft: `mc.example.com` · port `19132`",
        "🧛 V Rising: `203.0.113.7`",
        "🌿 Vintage Story: `vs.example.com:42420`",
    ]


async def test_ip_click_rechecks_the_clicker(monkeypatch, _hosts):
    _ip_level(5)
    _set_level(1, 5)
    view = addr_mod.address_view(_ctx("vrising"), ("vrising",))

    _set_level(2, 4)
    click, interaction = _click(view, uid=2)
    await click
    assert "level 5" in _private_text(interaction)

    _state.blocklist[(42, 1)] = {"reason": "t", "banned_by": 9, "banned_at": None}
    click, interaction = _click(view, uid=1)
    await click
    assert "203.0.113.7" not in _private_text(interaction)

    del _state.blocklist[(42, 1)]
    _ip_level(None)
    click, interaction = _click(view, uid=1)
    await click
    assert _private_text(interaction) == "Server addresses aren't shared here."


async def test_ip_button_survives_a_restart(_hosts):
    item = await addr_mod.ShowAddressButton.from_custom_id(
        None, None, re.fullmatch(addr_mod.ShowAddressButton.__discord_ui_compiled_template__, "srvaddr:minecraft,vrising"))
    assert item.keys == ("minecraft", "vrising")


async def test_public_ip_falls_back_when_the_host_resolves_to_the_lan(monkeypatch):
    async def _lan(*a, **k):
        return [(None, None, None, None, ("192.168.1.20", 0))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _lan)

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def raise_for_status(self):
            pass

        async def text(self):
            return "198.51.100.4\n"

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def get(self, url, timeout):
            assert url == addr_mod.PUBLIC_IP_URL
            return _Resp()
    monkeypatch.setattr(addr_mod.aiohttp, "ClientSession", _Session)
    assert await addr_mod._public_ip("vr.example.com") == "198.51.100.4"


async def test_settings_server_ip_sets_and_clears(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx("settings server-ip")
    await cog.settings_server_ip.callback(cog, ctx, "15")
    assert _state.guild_settings["42"]["server_ip_min_level"] == 15
    await cog.settings_server_ip.callback(cog, ctx, "0")
    assert "Usage:" in ctx.sent_embeds[-1].description
    await cog.settings_server_ip.callback(cog, ctx, "off")
    assert _state.guild_settings["42"]["server_ip_min_level"] is None
