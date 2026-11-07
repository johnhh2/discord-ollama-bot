"""V Rising / Vintage Story server notices (src/cogs/game_servers_cog.py)."""
import asyncio
import struct

import pytest

import src.state as _state
import src.cogs.game_servers_cog as gs_mod
from src.cogs.game_servers_cog import GameServer, GameServersCog, ServerStatus, _parse_a2s_info
from src.cogs.settings_cog import SettingsCog
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


# ── Monitor ──────────────────────────────────────────────────────────────────

async def test_vrising_join_notices_follow_each_guilds_switch(monkeypatch):
    quiet, loud = FakeTextChannel(ch_id=1), FakeTextChannel(ch_id=2)
    bot = _FakeBot([FakeGuild(gid=10), FakeGuild(gid=20)], {1: quiet, 2: loud})
    _state.guild_settings["10"] = {"vrising_channel": 1, "vrising_join_notices": False}
    _state.guild_settings["20"] = {"vrising_channel": 2}
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
    _state.guild_settings["10"] = {"vrising_channel": 1}
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
    _state.guild_settings["10"] = {"vintagestory_channel": 1}
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


# ── Settings ─────────────────────────────────────────────────────────────────

def _admin_ctx(name, channel_mentions=()):
    ctx = FakeCtx(author=FakeMember(1, administrator=True), guild=FakeGuild(gid=42), command_name=name)
    ctx.message.channel_mentions = list(channel_mentions)
    return ctx


async def test_settings_game_channels_set_and_clear(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx("settings channel vrising", [FakeTextChannel(ch_id=555)])
    await cog.settings_vrising_channel.callback(cog, ctx)
    assert _state.guild_settings["42"]["vrising_channel"] == 555
    await cog.settings_vintagestory_channel.callback(cog, _admin_ctx("settings channel vintagestory", [FakeTextChannel(ch_id=556)]))
    assert _state.guild_settings["42"]["vintagestory_channel"] == 556
    await cog.settings_vrising_channel.callback(cog, _admin_ctx("settings channel vrising"), "clear")
    assert _state.guild_settings["42"]["vrising_channel"] is None


async def test_settings_vrising_joins_toggles(db):
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx("settings vrising-joins")
    await cog.settings_vrising_joins.callback(cog, ctx, "off")
    assert _state.guild_settings["42"]["vrising_join_notices"] is False
    await cog.settings_vrising_joins.callback(cog, ctx, "maybe")
    assert "Usage:" in ctx.sent_embeds[-1].description
