"""!announce: argument parsing, the Wyoming client against a scripted fake
server, the PCM conversion, and the command flow with the voice connection
stubbed (connect / play / disconnect can't run without Discord)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.cogs.announce_cog as _announce_mod
from src.cogs.announce_cog import AnnounceCog, parse_announce_args
from src.settings_views import Field, _component
from src.tts import Audio, TTSError, WyomingTTS, encode_event, read_event, to_discord_pcm
from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

_aio = pytest.mark.asyncio

GUILD_ID = 42
GENERAL_ID = 100
GAMES_ID = 101


# ── argument parsing ─────────────────────────────────────────────────────────

def test_parse_quoted_message_in_either_order():
    assert parse_announce_args('"Dinner is ready" General') == ("Dinner is ready", "General")
    assert parse_announce_args('General "Dinner is ready"') == ("Dinner is ready", "General")
    assert parse_announce_args('🔊 General 2 "hi"') == ("hi", "🔊 General 2")
    assert parse_announce_args('“smart quotes” Lounge') == ("smart quotes", "Lounge")
    assert parse_announce_args('"only a message"') == ("only a message", "")
    assert parse_announce_args("General") == ("", "General")
    assert parse_announce_args("") == ("", "")


# ── PCM conversion ───────────────────────────────────────────────────────────

def test_to_discord_pcm_resamples_and_widens():
    one_second = b"\x00\x01" * 22050  # 16-bit mono at Piper's 22050 Hz
    out = to_discord_pcm(one_second, 22050, 2, 1)
    assert abs(len(out) - 48000 * 2 * 2) <= 32  # a second of 48 kHz 16-bit stereo, ± a few frames

    out8 = to_discord_pcm(b"\x80" * 8000, 8000, 1, 1)  # 8-bit mono
    assert abs(len(out8) - 48000 * 2 * 2) <= 32

    already = b"\x00" * 4800
    assert to_discord_pcm(already, 48000, 2, 2) == already

    with pytest.raises(TTSError):
        to_discord_pcm(b"\x00" * 12, 48000, 2, 6)


# ── the Wyoming client ───────────────────────────────────────────────────────

async def _fake_server(script):
    """One-shot server: reads the request event, hands it to `script(writer,
    type, data, payload)`, closes. Returns `(server, port)`."""
    async def handle(reader, writer):
        type_, data, payload = await read_event(reader)
        await script(writer, type_, data, payload)
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@_aio
async def test_synthesize_collects_the_audio_chunks():
    seen = {}
    fmt = {"rate": 22050, "width": 2, "channels": 1}

    async def script(writer, type_, data, payload):
        seen.update(type=type_, data=data)
        writer.write(encode_event("audio-start", fmt))
        writer.write(encode_event("audio-chunk", fmt, b"\x01\x02" * 10))
        writer.write(encode_event("audio-chunk", fmt, b"\x03\x04" * 10))
        writer.write(encode_event("audio-stop"))

    server, port = await _fake_server(script)
    try:
        audio = await WyomingTTS("127.0.0.1", port, voice="en_US-amy").synthesize("hello")
    finally:
        server.close()
        await server.wait_closed()
    assert seen["type"] == "synthesize"
    assert seen["data"] == {"text": "hello", "voice": {"name": "en_US-amy"}}
    assert (audio.rate, audio.width, audio.channels) == (22050, 2, 1)
    assert audio.pcm == b"\x01\x02" * 10 + b"\x03\x04" * 10


@_aio
async def test_synthesize_reports_server_errors_and_silence():
    async def error_script(writer, *_):
        writer.write(encode_event("error", {"text": "no such voice"}))

    server, port = await _fake_server(error_script)
    try:
        with pytest.raises(TTSError, match="no such voice"):
            await WyomingTTS("127.0.0.1", port).synthesize("hello")
    finally:
        server.close()
        await server.wait_closed()

    async def silent_script(writer, *_):
        writer.write(encode_event("audio-stop"))

    server, port = await _fake_server(silent_script)
    try:
        with pytest.raises(TTSError, match="no audio"):
            await WyomingTTS("127.0.0.1", port).synthesize("hello")
    finally:
        server.close()
        await server.wait_closed()

    # Nothing listens on that port any more: unreachable is a TTSError too.
    with pytest.raises(TTSError, match="can't reach"):
        await WyomingTTS("127.0.0.1", port).synthesize("hello")


@_aio
async def test_read_event_accepts_inline_data_and_the_data_length_form():
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"type": "audio-start", "data": {"rate": 16000}}).encode() + b"\n")
    reader.feed_data(encode_event("audio-chunk", {"rate": 16000}, b"\x00\x01"))
    reader.feed_eof()
    assert await read_event(reader) == ("audio-start", {"rate": 16000}, b"")
    assert await read_event(reader) == ("audio-chunk", {"rate": 16000}, b"\x00\x01")
    with pytest.raises(TTSError):
        await read_event(reader)


def test_voice_channel_field_is_a_voice_only_picker():
    select = _component(Field("channel", "Voice channel", kind="voice_channels"))
    assert isinstance(select, discord.ui.ChannelSelect)
    assert select.channel_types == [discord.ChannelType.voice]


# ── the command ──────────────────────────────────────────────────────────────

class _FakeTTS:
    def __init__(self):
        self.calls = []
        self.audio = Audio(b"\x00\x00" * 4800, 48000, 2, 2)  # 50 ms, already Discord's format

    async def synthesize(self, text):
        self.calls.append(text)
        return self.audio


class _FakeVoiceClient:
    def __init__(self):
        self.played = None
        self.disconnect = AsyncMock()
        self.stopped = False

    def play(self, source, after=None):
        self.played = source
        after(None)  # discord.py calls it from the player thread once the clip ends

    def stop(self):
        self.stopped = True


def _voice(cid, name, members=(), *, connect=True, speak=True, kind=discord.ChannelType.voice):
    ch = SimpleNamespace(id=cid, name=name, type=kind, members=list(members), mention=f"<#{cid}>")
    ch.permissions_for = lambda member: SimpleNamespace(connect=connect, speak=speak)
    ch.vc = _FakeVoiceClient()

    async def _connect(**kwargs):
        ch.connect_kwargs = kwargs
        return ch.vc
    ch.connect = _connect
    return ch


def _listener(uid=9):
    return SimpleNamespace(id=uid, bot=False)


def _ctx(channels=()):
    guild = FakeGuild(gid=GUILD_ID)
    guild.channels = guild.voice_channels = list(channels)
    guild.me = FakeMember(uid=1, display_name="bot")
    guild.voice_client = None
    return FakeCtx(author=FakeMember(uid=7, display_name="Xeph", administrator=True), guild=guild,
                   command_name="announce")


@pytest.fixture
def _no_builtin_channel_converter(monkeypatch):
    """discord.py's converter needs a real guild cache; let the project
    converter's name match over ctx.guild.voice_channels do the work."""
    import discord.ext.commands as _c

    async def _missing(self, ctx, argument):
        raise _c.ChannelNotFound(argument)
    monkeypatch.setattr(_c.VoiceChannelConverter, "convert", _missing)


def _cog(tts="fake"):
    return AnnounceCog(bot=SimpleNamespace(), tts=_FakeTTS() if tts == "fake" else tts)


def _titles(ctx):
    return [e.title or "" for e in ctx.sent_embeds]


@_aio
async def test_refuses_when_unconfigured_or_in_a_dm():
    cog = AnnounceCog(bot=SimpleNamespace(), tts=None)
    ctx = _ctx()
    await cog.cmd_announce.callback(cog, ctx, args='"hi" General')
    assert any("aren't configured" in (e.description or "") for e in ctx.sent_embeds)

    cog = _cog()
    ctx = _ctx()
    ctx.guild = None
    await cog.cmd_announce.callback(cog, ctx, args='"hi" General')
    assert any("not in DMs" in (e.description or "") for e in ctx.sent_embeds)
    assert cog.tts.calls == []


@_aio
async def test_typed_message_and_channel_in_either_order(_no_builtin_channel_converter):
    for args in ('"Dinner is ready" gen', 'GENERAL "Dinner is ready"'):
        cog = _cog()
        general = _voice(GENERAL_ID, "🔊 General", [_listener()])
        games = _voice(GAMES_ID, "Games", [_listener()])
        ctx = _ctx([general, games])

        await cog.cmd_announce.callback(cog, ctx, args=args)

        assert cog.tts.calls == ["Dinner is ready"]
        assert general.connect_kwargs == {"timeout": _announce_mod.CONNECT_TIMEOUT, "self_deaf": True}
        assert isinstance(general.vc.played, discord.PCMAudio)
        general.vc.disconnect.assert_awaited_once_with(force=True)
        assert not hasattr(games, "connect_kwargs")
        done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
        assert "🔊 General" in done.description and "Dinner is ready" in done.description
        assert cog._busy == set()


@_aio
async def test_missing_pieces_come_from_the_form(_no_builtin_channel_converter, monkeypatch):
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    games = _voice(GAMES_ID, "Games", [_listener()])
    forms = []

    async def _form(ctx, *, fields, **kwargs):
        forms.append(fields)
        return {"channel": [GAMES_ID], "text": "From the form"}
    monkeypatch.setattr(_announce_mod, "open_form", _form)

    # A lone unquoted argument that names a channel: only the message is asked for,
    # with that channel preselected.
    cog = _cog()
    ctx = _ctx([general, games])
    await cog.cmd_announce.callback(cog, ctx, args="gen")
    assert [f.kind for f in forms[-1]] == ["voice_channels", "paragraph"]
    assert forms[-1][0].defaults == (str(GENERAL_ID),)
    assert cog.tts.calls == ["From the form"]
    assert games.vc.played is not None  # the form's channel pick wins

    # A lone unquoted argument that names nobody is the message; the channel is asked for.
    cog = _cog()
    ctx = _ctx([general, games])
    await cog.cmd_announce.callback(cog, ctx, args="Dinner is ready")
    assert forms[-1][0].defaults == ()
    assert forms[-1][1].default == "Dinner is ready"

    # Bare !announce from inside a voice channel preselects that channel.
    cog = _cog()
    ctx = _ctx([general, games])
    ctx.author.voice = SimpleNamespace(channel=general)
    await cog.cmd_announce.callback(cog, ctx, args=None)
    assert forms[-1][0].defaults == (str(GENERAL_ID),)
    assert forms[-1][1].default is None

    # A declined form does nothing.
    async def _declined(ctx, **kwargs):
        return None
    monkeypatch.setattr(_announce_mod, "open_form", _declined)
    cog = _cog()
    ctx = _ctx([general, games])
    await cog.cmd_announce.callback(cog, ctx, args=None)
    assert cog.tts.calls == [] and ctx.sent_embeds == []


@_aio
async def test_declined_channel_picker_stays_quiet(_no_builtin_channel_converter):
    """A quoted message with a channel that matches nothing opens the channel
    picker (declined by conftest's default) — the closed prompt is the reply."""
    cog = _cog()
    ctx = _ctx([_voice(GENERAL_ID, "🔊 General", [_listener()])])
    await cog.cmd_announce.callback(cog, ctx, args='"hi" Nowhere')
    assert ctx.sent_embeds == [] and cog.tts.calls == []


@_aio
async def test_refusals_before_any_synthesis(_no_builtin_channel_converter):
    cases = {
        "empty": (_voice(GENERAL_ID, "General", []), "Nobody is in"),
        "bots only": (_voice(GENERAL_ID, "General", [SimpleNamespace(id=2, bot=True)]), "Nobody is in"),
        "no speak": (_voice(GENERAL_ID, "General", [_listener()], speak=False), "can't connect and speak"),
        "stage": (_voice(GENERAL_ID, "General", [_listener()], kind=discord.ChannelType.stage_voice), "isn't a voice channel"),
    }
    for label, (channel, expected) in cases.items():
        cog = _cog()
        ctx = _ctx([channel])
        await cog.cmd_announce.callback(cog, ctx, args='"hi" General')
        assert any(expected in (e.description or "") for e in ctx.sent_embeds), label
        assert cog.tts.calls == [], label
        assert not hasattr(channel, "connect_kwargs"), label

    cog = _cog()
    channel = _voice(GENERAL_ID, "General", [_listener()])
    ctx = _ctx([channel])
    await cog.cmd_announce.callback(cog, ctx, args='"' + "x" * (_announce_mod.ANNOUNCE_MAX_CHARS + 1) + '" General')
    assert any("Too Long" in t for t in _titles(ctx))
    assert cog.tts.calls == []


@_aio
async def test_one_announcement_per_guild_at_a_time(_no_builtin_channel_converter):
    class _SlowTTS(_FakeTTS):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()

        async def synthesize(self, text):
            self.calls.append(text)
            await self.release.wait()
            return self.audio

    cog = _cog(tts=_SlowTTS())
    general = _voice(GENERAL_ID, "General", [_listener()])
    games = _voice(GAMES_ID, "Games", [_listener()])
    ctx1, ctx2 = _ctx([general, games]), _ctx([general, games])

    first = asyncio.create_task(cog.cmd_announce.callback(cog, ctx1, args='"one" General'))
    await asyncio.sleep(0)
    await cog.cmd_announce.callback(cog, ctx2, args='"two" Games')
    assert any("already playing" in (e.description or "") for e in ctx2.sent_embeds)
    assert cog.tts.calls == ["one"]

    cog.tts.release.set()
    await first
    assert "📢 Announced" in _titles(ctx1)
    assert cog._busy == set()


@_aio
async def test_tts_and_connect_failures_release_the_slot(_no_builtin_channel_converter):
    class _BrokenTTS(_FakeTTS):
        async def synthesize(self, text):
            raise TTSError("can't reach the TTS server")

    cog = _cog(tts=_BrokenTTS())
    general = _voice(GENERAL_ID, "General", [_listener()])
    ctx = _ctx([general])
    await cog.cmd_announce.callback(cog, ctx, args='"hi" General')
    assert any("can't reach the TTS server" in (e.description or "") for e in ctx.sent_embeds)
    assert not hasattr(general, "connect_kwargs")
    assert cog._busy == set()

    cog = _cog()
    general = _voice(GENERAL_ID, "General", [_listener()])

    async def _timeout(**kwargs):
        raise asyncio.TimeoutError
    general.connect = _timeout
    ctx = _ctx([general])
    await cog.cmd_announce.callback(cog, ctx, args='"hi" General')
    assert any("Couldn't join" in (e.description or "") for e in ctx.sent_embeds)
    assert cog._busy == set()


@_aio
async def test_leftover_voice_connection_is_dropped_first(_no_builtin_channel_converter):
    cog = _cog()
    general = _voice(GENERAL_ID, "General", [_listener()])
    ctx = _ctx([general])
    leftover = SimpleNamespace(disconnect=AsyncMock())
    ctx.guild.voice_client = leftover

    await cog.cmd_announce.callback(cog, ctx, args='"hi" General')
    leftover.disconnect.assert_awaited_once_with(force=True)
    assert general.vc.played is not None


@_aio
async def test_play_waits_for_the_player_and_bounds_the_wait(monkeypatch):
    """The clip's end is signalled from the player thread; a player that never
    reports back is stopped after the clip length plus the grace period."""
    vc = _FakeVoiceClient()
    await AnnounceCog._play(vc, b"\x00" * 1920)  # 10 ms: `after` fires at once
    assert isinstance(vc.played, discord.PCMAudio) and not vc.stopped

    class _Stuck(_FakeVoiceClient):
        def play(self, source, after=None):
            self.played = source  # never calls `after`

    monkeypatch.setattr(_announce_mod, "PLAY_GRACE_SECS", 0.0)
    stuck = _Stuck()
    await AnnounceCog._play(stuck, b"\x00" * 192)  # 1 ms
    assert stuck.stopped
