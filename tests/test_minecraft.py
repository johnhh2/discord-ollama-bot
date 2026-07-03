"""Minecraft Bedrock status cog: !mc, the monitor fold, announce, presence.

No network anywhere — fetch_mc_status is monkeypatched at module scope
(src.cogs.minecraft_cog.fetch_mc_status), and the monitor loop is driven by
awaiting its .coro directly instead of starting the tasks.Loop.
"""
import json
import time
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
    assert fields["World"] == "My Server"    # server name, not the level name
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


async def test_mc_hides_address_by_default(monkeypatch):
    cog = _make_cog(monkeypatch, host="secret.example.com")
    monkeypatch.setattr(mc_mod, "MC_SERVER_SHOW_IP", False)

    async def _fake_fetch():
        return _status()
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    ctx = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx)
    online = ctx.sent_embeds[0]
    assert "secret.example.com" not in (online.description or "")

    async def _fake_fetch_down():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch_down)

    ctx2 = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx2)
    offline = ctx2.sent_embeds[0]
    assert "secret.example.com" not in offline.description
    assert "The Minecraft server didn't respond" in offline.description


async def test_mc_shows_address_when_opted_in(monkeypatch):
    cog = _make_cog(monkeypatch, host="secret.example.com")
    monkeypatch.setattr(mc_mod, "MC_SERVER_SHOW_IP", True)
    monkeypatch.setattr(mc_mod, "MC_SERVER_PORT", 19132)

    async def _fake_fetch():
        return _status()
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    ctx = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx)
    assert "secret.example.com:19132" in ctx.sent_embeds[0].description


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


async def test_monitor_count_change_embeds(monkeypatch):
    channel = FakeTextChannel(ch_id=555)
    bot = _FakeBot(guilds=[FakeGuild(gid=1)], channels={555: channel})
    _state.guild_settings["1"] = {"minecraft_channel": 555}

    cog = _make_cog(monkeypatch, bot=bot)
    cog._monitor = MonitorState(online=True, count=2)

    results = iter([_status(players=3), _status(players=1)])

    async def _fake_fetch():
        return next(results)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)   # 2 -> 3: join
    await _tick(cog)   # 3 -> 1: leave

    join = channel.send.call_args_list[0].kwargs["embed"]
    assert "Joined" in join.title
    assert "A player joined" in join.description
    assert "3/10" in join.description
    assert join.color.value == mc_mod.C_GREEN

    leave = channel.send.call_args_list[1].kwargs["embed"]
    assert "Left" in leave.title
    assert "2 players left" in leave.description
    assert "1/10" in leave.description
    assert leave.color.value == mc_mod.C_RED


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
    assert activity.name == "⛏️ 3/10 on Minecraft"


async def test_no_monitor_or_presence_when_unconfigured(monkeypatch):
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", "")
    bot = _FakeBot(guilds=[])
    cog = mc_mod.MinecraftCog(bot)
    assert not cog.mc_monitor.is_running()
    bot.change_presence.assert_not_called()


# ── Derived stats: online since, uptime %, avg ping ──────────────────────────

def _sample(ts, online, ping=None):
    return mc_mod.McSample(ts=ts, online=online, latency_ms=ping)


async def test_uptime_pct_counts_only_last_week():
    now = 1_000_000.0
    week = mc_mod.MC_UPTIME_WINDOW_SECS
    samples = [
        _sample(now - week - 100, False),          # outside window: ignored
        _sample(now - 3000, True, 40.0),
        _sample(now - 2000, False),
        _sample(now - 1000, True, 50.0),
        _sample(now, True, 60.0),
    ]
    pct, window_start = mc_mod._uptime_pct(samples, now)
    assert pct == 75.0
    assert window_start == now - 3000
    assert mc_mod._uptime_pct([], now) is None


async def test_avg_ping_uses_last_hour_of_up_samples():
    now = 1_000_000.0
    samples = [
        _sample(now - 7200, True, 500.0),          # older than an hour: ignored
        _sample(now - 1800, True, 40.0),
        _sample(now - 900, False),                 # down sample: ignored
        _sample(now - 60, True, 60.0),
    ]
    assert mc_mod._avg_ping_ms(samples, now) == 50.0
    assert mc_mod._avg_ping_ms([_sample(now, False)], now) is None


async def test_monitor_tracks_online_since_and_samples(monkeypatch):
    bot = _FakeBot(guilds=[])
    cog = _make_cog(monkeypatch, bot=bot)

    async def _fake_fetch():
        return _status(players=1, latency=30)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)
    assert cog._online_since is not None
    assert len(cog._samples) == 1 and cog._samples[0].online

    async def _fake_fetch_down():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch_down)

    await _tick(cog)
    await _tick(cog)                                # debounce → confirmed offline
    assert cog._online_since is None
    assert [s.online for s in cog._samples] == [True, False, False]


async def test_mc_embed_shows_derived_stats(monkeypatch):
    cog = _make_cog(monkeypatch)
    now = time.time()
    cog._online_since = now - 5000
    cog._samples.extend([
        _sample(now - 120, True, 40.0),
        _sample(now - 60, True, 60.0),
    ])

    async def _fake_fetch():
        return _status()
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    ctx = _mc_ctx()
    await cog.cmd_mc.callback(cog, ctx)

    embed = ctx.sent_embeds[0]
    assert embed.title == "🟢 Minecraft Server Status"
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Online since"] == f"<t:{int(now - 5000)}:R>"
    assert fields["Avg ping (1h)"] == "50 ms"
    assert fields["Uptime"] == "100.0% since <t:{}:R>".format(int(now - 120))


# ── !help fun section ─────────────────────────────────────────────────────────

async def _help_fun_field(monkeypatch, host: str) -> str:
    import src.cogs.utility_cog as util_mod
    monkeypatch.setattr(util_mod, "MC_SERVER_HOST", host)
    sent_embeds = []

    async def _fake_ephemeral(ctx, *args, **kwargs):
        sent_embeds.append(kwargs.get("embed"))
    monkeypatch.setattr(util_mod, "send_ephemeral", _fake_ephemeral)

    cog = util_mod.UtilityCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=42))
    ctx.command.qualified_name = "help"
    await cog.cmd_help.callback(cog, ctx)
    embed = sent_embeds[0]
    return next(f.value for f in embed.fields if "Fun" in f.name)


async def test_help_hides_minecraft_when_unconfigured(monkeypatch):
    fun = await _help_fun_field(monkeypatch, host="")
    assert "!minecraft" not in fun


async def test_help_shows_minecraft_when_configured(monkeypatch):
    fun = await _help_fun_field(monkeypatch, host="mc.example.com")
    assert "!minecraft" in fun


# ── !graph minecraft series ───────────────────────────────────────────────────

async def test_monitor_publishes_state_sample(monkeypatch):
    bot = _FakeBot(guilds=[])
    cog = _make_cog(monkeypatch, bot=bot)

    async def _fake_fetch():
        return _status(players=3, latency=55)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)

    await _tick(cog)
    assert _state.mc_last_online is True
    assert _state.mc_last_ping_ms == 55.0

    async def _fake_fetch_down():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch_down)

    await _tick(cog)
    await _tick(cog)
    assert _state.mc_last_online is False
    assert _state.mc_last_ping_ms is None


def _patch_graph_loads(monkeypatch, history=None, samples=None):
    from src import graph_series

    async def _fake_history():
        return history or {}

    async def _fake_samples(since_ts):
        return samples or []
    monkeypatch.setattr(graph_series, "load_bot_stats_history", _fake_history)
    monkeypatch.setattr(graph_series, "load_mc_ping_samples", _fake_samples)
    return graph_series


async def test_build_series_minecraft_points(monkeypatch):
    history = {
        "2026-07-01": {
            0: {"mc_up": True, "mc_ping_ms": 40.0},
            1: {"mc_up": False, "mc_ping_ms": None},   # downtime → 0
            2: {},                                     # predates mc_* keys → skipped
            3: {"mc_up": None, "mc_ping_ms": None},    # unknown → skipped
        },
    }
    graph_series = _patch_graph_loads(monkeypatch, history=history)
    monkeypatch.setattr(_state, "mc_last_online", None)  # no live point

    data = await graph_series.build_series_minecraft()

    assert len(data.x_points) == 2
    assert data.segments[0].y_values == [40.0, 0.0]
    assert data.native_style == "line"


async def test_build_series_minecraft_live_point(monkeypatch):
    graph_series = _patch_graph_loads(monkeypatch)
    monkeypatch.setattr(_state, "mc_last_online", True)
    monkeypatch.setattr(_state, "mc_last_ping_ms", 33.0)

    data = await graph_series.build_series_minecraft()
    assert data.segments[0].y_values == [33.0]

    monkeypatch.setattr(_state, "mc_last_online", False)
    monkeypatch.setattr(_state, "mc_last_ping_ms", None)
    data = await graph_series.build_series_minecraft()
    assert data.segments[0].y_values == [0.0]


async def test_build_series_minecraft_merges_buckets_and_samples(monkeypatch):
    import datetime as _dt
    from src.graph_series import _bucket_start_dt

    now = int(time.time())
    samples = [
        (now - 180, True, 42.0),
        (now - 120, False, None),      # down poll → 0
        (now - 60, True, 44.0),
    ]
    first_sample_dt = _dt.datetime.fromtimestamp(now - 180, tz=_dt.timezone.utc)
    # One bucket safely older than the samples, one inside their coverage.
    old_day = (first_sample_dt - _dt.timedelta(days=3)).date().isoformat()
    new_day = (first_sample_dt + _dt.timedelta(days=1)).date().isoformat()
    history = {
        old_day: {0: {"mc_up": True, "mc_ping_ms": 40.0}},
        new_day: {3: {"mc_up": True, "mc_ping_ms": 99.0}},   # covered → dropped
    }
    assert _bucket_start_dt(new_day, 3) >= first_sample_dt

    graph_series = _patch_graph_loads(monkeypatch, history=history, samples=samples)
    data = await graph_series.build_series_minecraft()

    # 1 old bucket point + all 3 per-poll samples; the covered bucket is gone.
    assert data.segments[0].y_values == [40.0, 42.0, 0.0, 44.0]
    assert data.x_points == sorted(data.x_points)


async def test_mc_ping_samples_roundtrip(db):
    from src.persistence import (
        save_mc_ping_sample, load_mc_ping_samples, prune_mc_ping_samples,
    )
    await save_mc_ping_sample(1000, True, 40.0)
    await save_mc_ping_sample(2000, False, None)
    await save_mc_ping_sample(3000, True, 60.0)

    rows = await load_mc_ping_samples(0)
    assert rows == [(1000, True, 40.0), (2000, False, None), (3000, True, 60.0)]
    assert await load_mc_ping_samples(2000) == rows[1:]

    await prune_mc_ping_samples(2000)
    assert await load_mc_ping_samples(0) == rows[1:]


async def test_monitor_persists_each_poll(monkeypatch):
    saved = []

    async def _spy_save(ts, online, latency_ms):
        saved.append((online, latency_ms))
    monkeypatch.setattr(_persistence, "save_mc_ping_sample", _spy_save)

    bot = _FakeBot(guilds=[])
    cog = _make_cog(monkeypatch, bot=bot)

    async def _fake_fetch():
        return _status(latency=30)
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch)
    await _tick(cog)

    async def _fake_fetch_down():
        return None
    monkeypatch.setattr(mc_mod, "fetch_mc_status", _fake_fetch_down)
    await _tick(cog)

    assert saved == [(True, 30.0), (False, None)]


async def test_restore_samples_rebuilds_window(monkeypatch):
    now = time.time()
    rows = [
        (int(now - 400), True, 40.0),
        (int(now - 300), False, None),
        (int(now - 120), True, 50.0),
        (int(now - 60), True, 60.0),
    ]

    async def _fake_load(since_ts):
        return rows
    monkeypatch.setattr(_persistence, "load_mc_ping_samples", _fake_load)

    cog = _make_cog(monkeypatch, bot=_FakeBot(guilds=[]))
    await cog._restore_samples()

    assert len(cog._samples) == 4
    # Trailing online run starts at the sample after the down poll.
    assert cog._online_since == float(rows[2][0])
    assert cog._last_seen_online == float(rows[3][0])


async def test_restore_samples_stale_gap_leaves_online_since_unset(monkeypatch):
    now = time.time()
    rows = [(int(now - 7200), True, 40.0)]   # bot was down for 2h — can't know

    async def _fake_load(since_ts):
        return rows
    monkeypatch.setattr(_persistence, "load_mc_ping_samples", _fake_load)

    cog = _make_cog(monkeypatch, bot=_FakeBot(guilds=[]))
    await cog._restore_samples()

    assert cog._online_since is None
    assert cog._last_seen_online == float(rows[0][0])


async def test_graph_registry_resolves_minecraft():
    from src.graph_series import find_spec
    spec = find_spec("minecraft")
    assert spec is not None and spec.name == "minecraft"
    assert find_spec("mcping") is spec
    # own group → cannot combine with the gateway ping series
    assert spec.group != find_spec("ping").group


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
