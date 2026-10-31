"""!announce: argument parsing, the Wyoming client against a scripted fake
server, the PCM conversion, and the command flow with the voice connection
stubbed (connect / play / disconnect can't run without Discord)."""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.audio_files as _audio_mod
import src.cogs.announce_cog as _announce_mod
import src.cogs.shop_cog as _shop_cog_mod
from src import state as _state
from src.economy import add_balance, get_balance
from src.guild_config import get_guild_cfg
from src.audio_files import (
    DecodeError, decode_audio, ffmpeg_available, is_audio_attachment, parse_ytdlp_output, youtube_url_in,
)
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
    assert parse_announce_args('"Dinner is ready" General') == ("Dinner is ready", "General", None)
    assert parse_announce_args('General "Dinner is ready"') == ("Dinner is ready", "General", None)
    assert parse_announce_args('🔊 General 2 "hi"') == ("hi", "🔊 General 2", None)
    assert parse_announce_args('“smart quotes” Lounge') == ("smart quotes", "Lounge", None)
    assert parse_announce_args('"only a message"') == ("only a message", "", None)
    assert parse_announce_args("General") == ("", "General", None)
    assert parse_announce_args("") == ("", "", None)


def test_parse_repeat_flag_anywhere_outside_the_quotes():
    assert parse_announce_args('"hi" General x5') == ("hi", "General", 5)
    assert parse_announce_args('x50 General "hi"') == ("hi", "General", 50)
    assert parse_announce_args('"hi" X3 Lounge 2') == ("hi", "Lounge 2", 3)
    assert parse_announce_args('"say x5 in here" General') == ("say x5 in here", "General", None)
    assert parse_announce_args("General x0") == ("", "General", 0)
    assert parse_announce_args("x2") == ("", "", 2)
    assert parse_announce_args("xray") == ("", "xray", None)


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
        self.plays = 0
        self.disconnect = AsyncMock()
        self.stopped = False

    def play(self, source, after=None):
        self.played = source
        self.plays += 1
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
                   command_name="adminannounce")


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
    await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" General')
    assert any("aren't configured" in (e.description or "") for e in ctx.sent_embeds)

    cog = _cog()
    ctx = _ctx()
    ctx.guild = None
    await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" General')
    assert any("not in DMs" in (e.description or "") for e in ctx.sent_embeds)
    assert cog.tts.calls == []


@_aio
async def test_typed_message_and_channel_in_either_order(_no_builtin_channel_converter):
    for args in ('"Dinner is ready" gen', 'GENERAL "Dinner is ready"'):
        cog = _cog()
        general = _voice(GENERAL_ID, "🔊 General", [_listener()])
        games = _voice(GAMES_ID, "Games", [_listener()])
        ctx = _ctx([general, games])

        await cog.cmd_adminannounce.callback(cog, ctx, args=args)

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
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen")
    assert [f.kind for f in forms[-1]] == ["voice_channels", "paragraph"]
    assert forms[-1][0].defaults == (str(GENERAL_ID),)
    assert cog.tts.calls == ["From the form"]
    assert games.vc.played is not None  # the form's channel pick wins

    # A lone unquoted argument that names nobody is the message; the channel is asked for.
    cog = _cog()
    ctx = _ctx([general, games])
    await cog.cmd_adminannounce.callback(cog, ctx, args="Dinner is ready")
    assert forms[-1][0].defaults == ()
    assert forms[-1][1].default == "Dinner is ready"

    # Bare !announce from inside a voice channel preselects that channel.
    cog = _cog()
    ctx = _ctx([general, games])
    ctx.author.voice = SimpleNamespace(channel=general)
    await cog.cmd_adminannounce.callback(cog, ctx, args=None)
    assert forms[-1][0].defaults == (str(GENERAL_ID),)
    assert forms[-1][1].default is None

    # A declined form does nothing.
    async def _declined(ctx, **kwargs):
        return None
    monkeypatch.setattr(_announce_mod, "open_form", _declined)
    cog = _cog()
    ctx = _ctx([general, games])
    await cog.cmd_adminannounce.callback(cog, ctx, args=None)
    assert cog.tts.calls == [] and ctx.sent_embeds == []


@_aio
async def test_declined_channel_picker_stays_quiet(_no_builtin_channel_converter):
    """A quoted message with a channel that matches nothing opens the channel
    picker (declined by conftest's default) — the closed prompt is the reply."""
    cog = _cog()
    ctx = _ctx([_voice(GENERAL_ID, "🔊 General", [_listener()])])
    await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" Nowhere')
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
        await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" General')
        assert any(expected in (e.description or "") for e in ctx.sent_embeds), label
        assert cog.tts.calls == [], label
        assert not hasattr(channel, "connect_kwargs"), label

    cog = _cog()
    channel = _voice(GENERAL_ID, "General", [_listener()])
    ctx = _ctx([channel])
    await cog.cmd_adminannounce.callback(cog, ctx, args='"' + "x" * (_announce_mod.ANNOUNCE_MAX_CHARS + 1) + '" General')
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

    first = asyncio.create_task(cog.cmd_adminannounce.callback(cog, ctx1, args='"one" General'))
    await asyncio.sleep(0)
    await cog.cmd_adminannounce.callback(cog, ctx2, args='"two" Games')
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
    await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" General')
    assert any("can't reach the TTS server" in (e.description or "") for e in ctx.sent_embeds)
    assert not hasattr(general, "connect_kwargs")
    assert cog._busy == set()

    cog = _cog()
    general = _voice(GENERAL_ID, "General", [_listener()])

    async def _timeout(**kwargs):
        raise asyncio.TimeoutError
    general.connect = _timeout
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" General')
    assert any("Couldn't join" in (e.description or "") for e in ctx.sent_embeds)
    assert cog._busy == set()


@_aio
async def test_leftover_voice_connection_is_dropped_first(_no_builtin_channel_converter):
    cog = _cog()
    general = _voice(GENERAL_ID, "General", [_listener()])
    ctx = _ctx([general])
    leftover = SimpleNamespace(disconnect=AsyncMock())
    ctx.guild.voice_client = leftover

    await cog.cmd_adminannounce.callback(cog, ctx, args='"hi" General')
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


# ── attached audio files ─────────────────────────────────────────────────────

def _attachment(filename="clip.mp3", content_type="audio/mpeg", size=1000, data=b"\x00" * 16):
    return SimpleNamespace(filename=filename, content_type=content_type, size=size,
                           read=AsyncMock(return_value=data))


def test_audio_attachments_by_content_type_or_extension():
    assert is_audio_attachment(_attachment("x", "audio/ogg; codecs=opus"))
    assert is_audio_attachment(_attachment("voice-message.ogg", None))
    assert is_audio_attachment(_attachment("Song.M4A", "application/octet-stream"))
    assert is_audio_attachment(_attachment("clip.mp4", "video/mp4"))  # its soundtrack plays
    assert is_audio_attachment(_attachment("clip.mkv", None))
    assert not is_audio_attachment(_attachment("photo.png", "image/png"))
    assert not is_audio_attachment(_attachment("notes.txt", None))


def _wav(seconds: float, rate=8000) -> bytes:
    """A mono 16-bit WAV of silence, built with the stdlib."""
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


@_aio
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not on PATH")
async def test_decode_audio_with_real_ffmpeg():
    frame = 48000 * 2 * 2
    pcm = await decode_audio(_wav(1.0), max_secs=10, filename="a.wav")
    assert abs(len(pcm) - frame) <= 4 * 48  # a second at Discord's rate, ± a few frames
    cut = await decode_audio(_wav(1.0), max_secs=0.5, filename="a.wav")
    assert abs(len(cut) - frame // 2) <= 4 * 48
    with pytest.raises(DecodeError):
        await decode_audio(b"not audio at all", max_secs=10, filename="a.mp3")


@_aio
async def test_decode_audio_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(_audio_mod, "FFMPEG", "ffmpeg-that-does-not-exist")
    with pytest.raises(DecodeError, match="isn't installed"):
        await decode_audio(b"\x00", max_secs=10)


@pytest.fixture
def _fake_decoder(monkeypatch):
    calls = []

    async def _decode(data, *, max_secs, filename=""):
        calls.append((data, max_secs, filename))
        return b"\x00\x00" * 4800  # 50 ms of Discord PCM
    monkeypatch.setattr(_announce_mod, "decode_audio", _decode)
    return calls


@_aio
async def test_attached_file_plays_instead_of_text(_no_builtin_channel_converter, _fake_decoder):
    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    clip = _attachment(data=b"mp3 bytes")
    ctx.message.attachments = [SimpleNamespace(filename="photo.png", content_type="image/png", size=1), clip]

    await cog.cmd_adminannounce.callback(cog, ctx, args="gen")

    assert _fake_decoder == [(b"mp3 bytes", _announce_mod.ANNOUNCE_MAX_SECS, "clip.mp3")]
    assert cog.tts.calls == []
    assert isinstance(general.vc.played, discord.PCMAudio)
    general.vc.disconnect.assert_awaited_once_with(force=True)
    done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
    assert "clip.mp3" in done.description and "🔊 General" in done.description
    assert cog._busy == set()


@_aio
async def test_attached_file_asks_only_for_the_channel(_no_builtin_channel_converter, _fake_decoder, monkeypatch):
    forms = []

    async def _form(ctx, *, fields, **kwargs):
        forms.append((fields, kwargs))
        return {"channel": [GENERAL_ID]}
    monkeypatch.setattr(_announce_mod, "open_form", _form)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    ctx.message.attachments = [_attachment()]
    await cog.cmd_adminannounce.callback(cog, ctx, args=None)

    fields, kwargs = forms[-1]
    assert [f.kind for f in fields] == ["voice_channels"]
    assert "clip.mp3" in kwargs["description"]
    assert general.vc.played is not None


@_aio
async def test_attached_file_refusals(_no_builtin_channel_converter, _fake_decoder):
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])

    cog = _cog()
    ctx = _ctx([general])
    ctx.message.attachments = [_attachment()]
    await cog.cmd_adminannounce.callback(cog, ctx, args='"and some words" gen')
    assert any("One thing to play" in (e.description or "") for e in ctx.sent_embeds)

    cog = _cog()
    ctx = _ctx([general])
    ctx.message.attachments = [_attachment(size=_announce_mod.ANNOUNCE_MAX_FILE_BYTES + 1)]
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen")
    assert any("Too Big" in t for t in _titles(ctx))

    assert _fake_decoder == [] and not hasattr(general, "connect_kwargs")


@_aio
async def test_undecodable_file_releases_the_slot(_no_builtin_channel_converter, monkeypatch):
    async def _broken(data, *, max_secs, filename=""):
        raise DecodeError("Invalid data found when processing input")
    monkeypatch.setattr(_announce_mod, "decode_audio", _broken)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    ctx.message.attachments = [_attachment()]
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen")
    assert any("Invalid data" in (e.description or "") for e in ctx.sent_embeds)
    assert not hasattr(general, "connect_kwargs")
    assert cog._busy == set()


@_aio
async def test_long_file_is_reported_as_cut(_no_builtin_channel_converter, monkeypatch):
    monkeypatch.setattr(_announce_mod, "ANNOUNCE_MAX_SECS", 1)
    monkeypatch.setattr(_announce_mod, "PLAY_GRACE_SECS", 0.0)

    async def _full_length(data, *, max_secs, filename=""):
        return b"\x00" * (48000 * 2 * 2 * max_secs)
    monkeypatch.setattr(_announce_mod, "decode_audio", _full_length)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    ctx.message.attachments = [_attachment()]
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen")
    done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
    assert "first 1 seconds" in done.description


# ── YouTube links ────────────────────────────────────────────────────────────

def test_youtube_url_is_lifted_out_of_the_arguments():
    assert youtube_url_in("gen https://www.youtube.com/watch?v=dQw4w9WgXcQ x2") == \
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "gen x2")
    assert youtube_url_in("https://youtu.be/dQw4w9WgXcQ?t=5 General") == ("https://youtu.be/dQw4w9WgXcQ?t=5", "General")
    assert youtube_url_in("<https://youtube.com/shorts/dQw4w9WgXcQ> gen") == ("https://youtube.com/shorts/dQw4w9WgXcQ", "gen")
    assert youtube_url_in("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x") == ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x", "")
    assert youtube_url_in("https://example.com/watch?v=dQw4w9WgXcQ gen") == (None, "https://example.com/watch?v=dQw4w9WgXcQ gen")
    assert youtube_url_in('"hi" gen') == (None, '"hi" gen')


def test_ytdlp_subprocesses_get_their_own_tmpdir(monkeypatch):
    """The standalone binary unpacks into TMPDIR and execs from there; the
    container's /tmp is noexec, so YTDLP_TMPDIR redirects only yt-dlp."""
    monkeypatch.setattr(_audio_mod, "YTDLP_TMPDIR", "")
    assert _audio_mod._ytdlp_env() is None
    monkeypatch.setattr(_audio_mod, "YTDLP_TMPDIR", "/run/ytdlp")
    env = _audio_mod._ytdlp_env()
    assert env["TMPDIR"] == "/run/ytdlp" and "PATH" in env


def test_parse_ytdlp_output():
    assert parse_ytdlp_output("Never Gonna Give You Up\n212\nFalse\nhttps://rr1.googlevideo.com/x\n") == \
        ("Never Gonna Give You Up", False, "https://rr1.googlevideo.com/x")
    assert parse_ytdlp_output("[warn] something\nLive now\nNA\nTrue\nhttps://m/x")[1] is True
    with pytest.raises(DecodeError):
        parse_ytdlp_output("ERROR: no video\n")
    with pytest.raises(DecodeError):
        parse_ytdlp_output("t\n1\nFalse\nnot a url\n")


@pytest.fixture
def _fake_youtube(monkeypatch):
    calls = []

    async def _fetch(url, *, max_secs):
        calls.append((url, max_secs))
        return b"\x00\x00" * 4800, "Never Gonna Give You Up"
    monkeypatch.setattr(_announce_mod, "fetch_youtube", _fetch)
    return calls


@_aio
async def test_youtube_link_plays_its_audio(_no_builtin_channel_converter, _fake_youtube, _fake_decoder):
    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen https://youtu.be/dQw4w9WgXcQ x2")

    assert _fake_youtube == [("https://youtu.be/dQw4w9WgXcQ", _announce_mod.ANNOUNCE_MAX_SECS)]
    assert cog.tts.calls == [] and _fake_decoder == []
    assert general.vc.plays == 2
    done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
    assert "Never Gonna Give You Up" in done.description and "2 times" in done.description
    assert cog._busy == set()


@_aio
async def test_youtube_link_asks_for_the_channel_and_refuses_mixes(_no_builtin_channel_converter, _fake_youtube, monkeypatch):
    forms = []

    async def _form(ctx, *, fields, **kwargs):
        forms.append((fields, kwargs))
        return {"channel": [GENERAL_ID]}
    monkeypatch.setattr(_announce_mod, "open_form", _form)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    fields, kwargs = forms[-1]
    assert [f.kind for f in fields] == ["voice_channels"] and "video" in kwargs["description"]
    assert general.vc.plays == 1

    for args, attachments in [
        ('"and words" gen https://youtu.be/dQw4w9WgXcQ', []),
        ("gen https://youtu.be/dQw4w9WgXcQ", [_attachment()]),
    ]:
        cog = _cog()
        ctx = _ctx([general])
        ctx.message.attachments = attachments
        await cog.cmd_adminannounce.callback(cog, ctx, args=args)
        assert any("One thing to play" in (e.description or "") for e in ctx.sent_embeds), args
    assert len(_fake_youtube) == 1


@_aio
async def test_youtube_failure_releases_the_slot(_no_builtin_channel_converter, monkeypatch):
    updates = []

    async def _update():
        updates.append(1)
        return "yt-dlp is up to date"
    monkeypatch.setattr(_announce_mod, "update_ytdlp", _update)

    async def _live(url, *, max_secs):
        raise DecodeError("that's a live stream")
    monkeypatch.setattr(_announce_mod, "fetch_youtube", _live)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen https://youtu.be/dQw4w9WgXcQ")
    assert any("live stream" in (e.description or "") for e in ctx.sent_embeds)
    assert not hasattr(general, "connect_kwargs") and cog._busy == set()
    assert updates == []  # a live stream isn't a stale-yt-dlp failure


@_aio
async def test_failed_resolve_updates_ytdlp_once_and_retries(_no_builtin_channel_converter, monkeypatch):
    """An extraction failure is usually yt-dlp predating a YouTube change:
    `yt-dlp -U` once, retry once, and not again within the hour."""
    updates, fetches = [], []

    async def _update():
        updates.append(1)
        return "Updated yt-dlp to 2026.11.01"
    monkeypatch.setattr(_announce_mod, "update_ytdlp", _update)

    async def _fetch(url, *, max_secs):
        fetches.append(url)
        if len(fetches) == 1:
            raise DecodeError("yt-dlp gave no media URL")
        return b"\x00\x00" * 4800, "Fixed after update"
    monkeypatch.setattr(_announce_mod, "fetch_youtube", _fetch)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen https://youtu.be/dQw4w9WgXcQ")
    assert updates == [1] and len(fetches) == 2
    assert any("Fixed after update" in (e.description or "") for e in ctx.sent_embeds)

    # Within the retry window a second failure is reported as is: no update, no retry.
    async def _broken(url, *, max_secs):
        fetches.append(url)
        raise DecodeError("yt-dlp gave no media URL")
    monkeypatch.setattr(_announce_mod, "fetch_youtube", _broken)
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen https://youtu.be/dQw4w9WgXcQ")
    assert updates == [1] and len(fetches) == 3
    assert any("no media URL" in (e.description or "") for e in ctx.sent_embeds)

    # Past the window it tries again; an update that can't run (read-only
    # install) just surfaces the original error.
    cog._ytdlp_update_at -= _announce_mod.YTDLP_UPDATE_RETRY_SECS + 1

    async def _cant_update():
        raise DecodeError("yt-dlp -U failed")
    monkeypatch.setattr(_announce_mod, "update_ytdlp", _cant_update)
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args="gen https://youtu.be/dQw4w9WgXcQ")
    assert len(fetches) == 4 and cog._busy == set()
    assert any("no media URL" in (e.description or "") for e in ctx.sent_embeds)


def test_ytdlp_updates_daily():
    assert AnnounceCog.update_ytdlp_daily.hours == 24


# ── x<N> repeats and !announce stop ──────────────────────────────────────────

@_aio
async def test_repeat_flag_plays_the_clip_n_times(_no_builtin_channel_converter, _fake_decoder):
    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args='"Dinner is ready" gen x3')
    assert cog.tts.calls == ["Dinner is ready"]  # synthesized once
    assert general.vc.plays == 3
    general.vc.disconnect.assert_awaited_once_with(force=True)
    done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
    assert "3 times" in done.description

    # A file repeats the same way; the decoder runs once.
    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    ctx.message.attachments = [_attachment()]
    await cog.cmd_adminannounce.callback(cog, ctx, args="x4 gen")
    assert len(_fake_decoder) == 1 and general.vc.plays == 4


@_aio
async def test_repeat_limits(_no_builtin_channel_converter):
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    cap = _announce_mod.ANNOUNCE_MAX_REPEATS
    total = _announce_mod.ANNOUNCE_MAX_TOTAL_CHARS

    for args, expected in {
        f'"hi" gen x{cap + 1}': f"x{cap}",
        '"hi" gen x0': f"x{cap}",
        '"' + "x" * 100 + '" gen x11': "repeat 10 times at most",  # 100 × 11 is over 1000
        '"' + "x" * 400 + '" gen x3': "repeat 2 times at most",
    }.items():
        cog = _cog()
        ctx = _ctx([general])
        await cog.cmd_adminannounce.callback(cog, ctx, args=args)
        assert any(expected in (e.description or "") for e in ctx.sent_embeds), args
        assert cog.tts.calls == [], args
    assert not hasattr(general, "connect_kwargs")

    # Exactly the cap is fine: 100 × 10.
    cog = _cog()
    ctx = _ctx([general])
    await cog.cmd_adminannounce.callback(cog, ctx, args='"' + "x" * 100 + '" gen x10')
    assert general.vc.plays == 10
    assert total == 1000  # the arithmetic above assumes it


@_aio
async def test_stop_cuts_a_repeat_short(_no_builtin_channel_converter):
    class _Held(_FakeVoiceClient):
        """Each clip plays until stop() is called, like a real player mid-clip."""
        def play(self, source, after=None):
            self.played = source
            self.plays += 1
            self._after = after

        def stop(self):
            self.stopped = True
            self._after(None)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    general.vc = _Held()
    ctx1, ctx2 = _ctx([general]), _ctx([general])

    # Nothing to stop yet.
    await cog.cmd_adminannounce.callback(cog, ctx2, args="stop")
    assert any("Nothing is playing" in (e.description or "") for e in ctx2.sent_embeds)

    task = asyncio.create_task(cog.cmd_adminannounce.callback(cog, ctx1, args='"hi" gen x50'))
    for _ in range(5):
        await asyncio.sleep(0)
    assert general.vc.plays == 1 and GUILD_ID in cog._busy

    await cog.cmd_adminannounce.callback(cog, ctx2, args="STOP")
    await task
    assert general.vc.stopped and general.vc.plays == 1
    done = next(e for e in ctx1.sent_embeds if "Announced" in (e.title or ""))
    assert "stopped after 1 of 50" in done.description
    assert cog._busy == set() and cog._stop == set() and cog._playing == {}


@_aio
async def test_announcestop_command_stops_anything(_no_builtin_channel_converter):
    """The admin-tier command, with no ownership check: a paid announcement
    started by someone else stops too."""
    class _Held(_FakeVoiceClient):
        def play(self, source, after=None):
            self.plays += 1
            self._after = after

        def stop(self):
            self.stopped = True
            self._after(None)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    general.vc = _Held()
    ctx1, ctx2 = _ctx([general]), _ctx([general])
    ctx2.author = FakeMember(uid=8, display_name="Mod")

    await cog.cmd_announcestop.callback(cog, ctx2)
    assert any("Nothing is playing" in (e.description or "") for e in ctx2.sent_embeds)

    task = asyncio.create_task(cog.cmd_adminannounce.callback(cog, ctx1, args='"hi" gen x50'))
    for _ in range(5):
        await asyncio.sleep(0)
    assert general.vc.plays == 1 and cog._owner.get(GUILD_ID) == ctx1.author.id

    await cog.cmd_announcestop.callback(cog, ctx2)
    await task
    assert general.vc.stopped and general.vc.plays == 1
    done = next(e for e in ctx1.sent_embeds if "Announced" in (e.title or ""))
    assert "stopped after 1 of 50" in done.description

    cog.tts = None
    ctx3 = _ctx([general])
    await cog.cmd_announcestop.callback(cog, ctx3)
    assert any("aren't configured" in (e.description or "") for e in ctx3.sent_embeds)


# ── !shop announce / !announce: the paid one ─────────────────────────────────

PAYER = 7  # _ctx's author


def test_announce_cost_is_per_character_times_repeats_up_to_the_cap():
    per, cap = _announce_mod.SHOP_ANNOUNCE_COST_PER_CHAR, _announce_mod.SHOP_ANNOUNCE_MAX_COST
    assert _announce_mod.announce_cost("hello") == 5 * per
    assert _announce_mod.announce_cost("hello", 3) == 15 * per
    assert _announce_mod.announce_cost("x" * _announce_mod.ANNOUNCE_MAX_TOTAL_CHARS) == cap
    assert _announce_mod.announce_cost("x" * 50, 10) == min(500 * per, cap)


def test_announce_form_args_quote_the_message_for_the_typed_parser():
    assert _announce_mod.announce_form_args('say "hi"', 100) == "\"say 'hi'\" 100"
    assert _announce_mod.announce_form_args(" dinner ", "100", 3) == '"dinner" 100 x3'
    assert parse_announce_args(_announce_mod.announce_form_args("dinner", 100, 3)) == ("dinner", "100", 3)


def test_paid_announce_is_a_shop_item_and_a_top_level_alias():
    import json
    from pathlib import Path
    from src.features import command_features

    perms = json.loads(Path("src/command_perms.json").read_text(encoding="utf-8"))
    assert perms["adminannounce"]["tier"] == "server_admin" and perms["announce"]["tier"] == "everyone"
    assert ("announce", "shop_announce", []) in _shop_cog_mod._SHOP_TOP_ALIASES
    assert command_features("announce") == ("shop",) and command_features("shop announce") == ("shop",)
    assert command_features("adminannounce") == ()  # the admin one stays on with the shop off


@_aio
async def test_shop_announce_forwards_to_the_paid_path():
    calls = []

    class _Announce:
        async def paid_announce(self, ctx, args):
            calls.append(args)
    bot = SimpleNamespace(get_cog=lambda name: _Announce() if name == "AnnounceCog" else None,
                          add_command=lambda c: None, remove_command=lambda n: None)
    cog = _shop_cog_mod.ShopCog(bot=bot)
    ctx = _ctx()
    await cog.shop_announce.callback(cog, ctx, args='"hi" gen')
    assert calls == ['"hi" gen']

    # `!settings shop announce off` takes it off this server's shelf.
    get_guild_cfg(GUILD_ID)["shop_items"] = {"announce": False}
    ctx = _ctx()
    await cog.shop_announce.callback(cog, ctx, args='"hi" gen')
    assert calls == ['"hi" gen'] and any("Disabled" in t for t in _titles(ctx))


@_aio
async def test_paid_announce_confirms_charges_and_plays(_no_builtin_channel_converter, monkeypatch):
    confirms = []

    async def _confirm(ctx, **kwargs):
        confirms.append(kwargs)
        return True
    monkeypatch.setattr(_announce_mod, "confirm_purchase", _confirm)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    ctx = _ctx([general])
    await add_balance(PAYER, 20_000)
    await cog.paid_announce(ctx, '"Dinner is ready" gen x2')

    cost = _announce_mod.announce_cost("Dinner is ready", 2)
    assert confirms[-1]["cost"] == cost and confirms[-1]["payer"] is ctx.author
    assert await get_balance(PAYER) == 20_000 - cost
    assert cog.tts.calls == ["Dinner is ready"] and general.vc.plays == 2
    done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
    assert f"{cost:,} 🪙" in done.description and "2 times" in done.description
    assert cog._busy == set() and cog._owner == {}


@_aio
async def test_paid_announce_declined_or_unaffordable_plays_nothing(_no_builtin_channel_converter, monkeypatch):
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    await add_balance(PAYER, 20_000)

    async def _declined(ctx, **kwargs):
        return False
    monkeypatch.setattr(_announce_mod, "confirm_purchase", _declined)
    cog = _cog()
    ctx = _ctx([general])
    await cog.paid_announce(ctx, '"hi" gen')
    assert cog.tts.calls == [] and await get_balance(PAYER) == 20_000

    async def _accepted(ctx, **kwargs):
        return True
    monkeypatch.setattr(_announce_mod, "confirm_purchase", _accepted)
    _state.economy["users"][str(PAYER)]["balance"] = 100
    cog = _cog()
    ctx = _ctx([general])
    await cog.paid_announce(ctx, '"hi" gen')
    assert any("Insufficient Funds" in t for t in _titles(ctx))
    assert cog.tts.calls == [] and not hasattr(general, "connect_kwargs")
    assert await get_balance(PAYER) == 100


@_aio
async def test_paid_announce_refuses_before_the_prompt(_no_builtin_channel_converter, monkeypatch):
    """Media, an empty channel and an over-cap message are refused before
    anyone is asked to pay."""
    confirms = []

    async def _confirm(ctx, **kwargs):
        confirms.append(1)
        return True
    monkeypatch.setattr(_announce_mod, "confirm_purchase", _confirm)
    await add_balance(PAYER, 200_000)
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    cog = _cog()

    ctx = _ctx([general])
    ctx.message.attachments = [_attachment()]
    await cog.paid_announce(ctx, "gen")
    assert any("for admins" in (e.description or "") for e in ctx.sent_embeds)

    ctx = _ctx([general])
    await cog.paid_announce(ctx, "gen https://youtu.be/dQw4w9WgXcQ")
    assert any("for admins" in (e.description or "") for e in ctx.sent_embeds)

    ctx = _ctx([_voice(GENERAL_ID, "🔊 General", [])])
    await cog.paid_announce(ctx, '"hi" gen')
    assert any("Nobody is in" in (e.description or "") for e in ctx.sent_embeds)

    ctx = _ctx([general])
    await cog.paid_announce(ctx, '"' + "x" * 400 + '" gen x3')
    assert any("repeat 2 times at most" in (e.description or "") for e in ctx.sent_embeds)

    assert confirms == [] and cog.tts.calls == []
    assert await get_balance(PAYER) == 200_000


@_aio
async def test_paid_announce_refunds_when_nothing_played(_no_builtin_channel_converter, monkeypatch):
    class _BrokenTTS(_FakeTTS):
        async def synthesize(self, text):
            raise TTSError("can't reach the TTS server")

    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    await add_balance(PAYER, 20_000)
    cog = _cog(tts=_BrokenTTS())
    ctx = _ctx([general])
    await cog.paid_announce(ctx, '"hi" gen')
    assert any("can't reach" in (e.description or "") for e in ctx.sent_embeds)
    assert any("Refunded" in t for t in _titles(ctx))
    assert await get_balance(PAYER) == 20_000 and cog._busy == set()

    # The listener left while the prompt was open: the re-check refuses and refunds.
    async def _leaves(ctx, **kwargs):
        general.members.clear()
        return True
    monkeypatch.setattr(_announce_mod, "confirm_purchase", _leaves)
    cog = _cog()
    ctx = _ctx([general])
    await cog.paid_announce(ctx, '"hi" gen')
    assert any("Nobody is in" in (e.description or "") for e in ctx.sent_embeds)
    assert any("Refunded" in t for t in _titles(ctx))
    assert await get_balance(PAYER) == 20_000 and cog.tts.calls == []


@_aio
async def test_godmode_announces_free(_no_builtin_channel_converter, monkeypatch):
    confirms = []

    async def _confirm(ctx, **kwargs):
        confirms.append(kwargs["cost"])
        return True
    monkeypatch.setattr(_announce_mod, "confirm_purchase", _confirm)
    _state.godmode_users.add(PAYER)
    try:
        cog = _cog()
        general = _voice(GENERAL_ID, "🔊 General", [_listener()])
        ctx = _ctx([general])
        await cog.paid_announce(ctx, '"hi" gen')
    finally:
        _state.godmode_users.discard(PAYER)
    assert confirms == [0] and general.vc.plays == 1
    done = next(e for e in ctx.sent_embeds if "Announced" in (e.title or ""))
    assert "Paid" not in done.description


@_aio
async def test_paid_stop_is_the_payers_or_an_admins(_no_builtin_channel_converter):
    class _Held(_FakeVoiceClient):
        def play(self, source, after=None):
            self.played = source
            self.plays += 1
            self._after = after

        def stop(self):
            self.stopped = True
            self._after(None)

    cog = _cog()
    general = _voice(GENERAL_ID, "🔊 General", [_listener()])
    general.vc = _Held()
    await add_balance(PAYER, 20_000)
    payer = _ctx([general])
    task = asyncio.create_task(cog.paid_announce(payer, '"hi" gen x5'))
    for _ in range(50):
        await asyncio.sleep(0)
        if general.vc.plays:
            break
    assert general.vc.plays == 1 and GUILD_ID in cog._busy

    stranger = _ctx([general])
    stranger.author = FakeMember(uid=8, display_name="Ann", administrator=False)
    await cog.paid_announce(stranger, "stop")
    assert any("someone else's" in (e.description or "") for e in stranger.sent_embeds)
    assert not general.vc.stopped

    await cog.paid_announce(_ctx([general]), "STOP")  # the payer again
    await task
    assert general.vc.stopped and general.vc.plays == 1
    done = next(e for e in payer.sent_embeds if "Announced" in (e.title or ""))
    assert "stopped after 1 of 5" in done.description
    # A stop is the payer's choice: no refund.
    assert await get_balance(PAYER) == 20_000 - _announce_mod.announce_cost("hi", 5)
    assert cog._busy == set() and cog._owner == {}
