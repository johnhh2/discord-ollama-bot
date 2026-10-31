"""!announce: read a message aloud in a voice channel.

The text goes to a Wyoming text-to-speech server (src/tts.py), the PCM comes
back, and the bot joins the channel, plays it and leaves. An attached audio
file is played instead of text (src/audio_files.py, ffmpeg) through the same
player. Off until WYOMING_TTS_HOST is set. Rules in CLAUDE.md: Voice
announcements.
"""
import asyncio
import io
import logging
import re
import time

import discord
from discord.ext import commands, tasks

from src.audio_files import (
    DecodeError, decode_audio, fetch_youtube, ffmpeg_available, is_audio_attachment, update_ytdlp,
    ytdlp_available, youtube_url_in,
)
from src.config import (
    WYOMING_TTS_HOST, WYOMING_TTS_PORT, WYOMING_TTS_VOICE,
    ANNOUNCE_MAX_CHARS, ANNOUNCE_MAX_FILE_BYTES, ANNOUNCE_MAX_SECS,
    ANNOUNCE_MAX_REPEATS, ANNOUNCE_MAX_TOTAL_CHARS,
)
from src.helpers import emb, VoiceChannelConverter, C_GREEN, C_RED, C_GREY
from src.settings_views import Field, open_form
from src.tts import WyomingTTS, TTSError, DISCORD_RATE, DISCORD_CHANNELS, SAMPLE_WIDTH

CONNECT_TIMEOUT = 10.0
PLAY_GRACE_SECS = 10.0  # allowed on top of the clip's own length before the player is given up on
YTDLP_UPDATE_RETRY_SECS = 3600.0  # a failed resolve triggers `yt-dlp -U` at most this often

# Straight or curly quotes — phones autocorrect to the latter.
_QUOTED_RE = re.compile(r'"([^"]*)"|“([^”]*)”')


_REPEAT_RE = re.compile(r"[xX×](\d+)")


def parse_announce_args(raw: str) -> tuple[str, str, "int | None"]:
    """`(message, channel_query, repeats)` from the typed arguments. The
    quoted part is the message, in either order; an `x<N>` token outside the
    quotes is the repeat count (None when absent); everything else is the
    channel. Without quotes the message is empty and the rest comes back as
    the channel query, for the caller to try as one."""
    m = _QUOTED_RE.search(raw)
    if m is None:
        text, rest = "", raw
    else:
        text = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
        rest = raw[:m.start()] + " " + raw[m.end():]
    repeats = None
    tokens = []
    for token in rest.split():
        r = _REPEAT_RE.fullmatch(token)
        if r is not None and repeats is None:
            repeats = int(r.group(1))
        else:
            tokens.append(token)
    return text, " ".join(tokens), repeats


def _opus_loaded() -> bool:
    """discord.py loads libopus lazily, on the first encoder; load it now so a
    missing library is a boot-time warning rather than the first !announce's
    error."""
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus._load_default()
    except Exception:
        return False
    return discord.opus.is_loaded()


class AnnounceCog(commands.Cog):
    def __init__(self, bot, tts=None):
        self.bot = bot
        if tts is None and WYOMING_TTS_HOST:
            tts = WyomingTTS(WYOMING_TTS_HOST, WYOMING_TTS_PORT, WYOMING_TTS_VOICE)
        self.tts = tts
        self._busy: set[int] = set()  # guilds with an announcement in progress (see CLAUDE.md: Concurrency)
        self._playing: dict[int, discord.VoiceClient] = {}  # guild → the client mid-clip, for !announce stop
        self._stop: set[int] = set()  # guilds whose running announcement was told to stop
        self._ytdlp_update_at = 0.0  # monotonic time of the last `yt-dlp -U` attempt

    async def cog_load(self):
        if self.tts is None:
            return
        # Voice needs three native pieces; each missing one is a boot
        # warning here rather than the first !announce's error.
        if not discord.voice_client.has_nacl:
            logging.warning("announce_nacl_missing: PyNaCl isn't installed, !announce will fail to connect")
        if not discord.voice_state.has_dave:
            logging.warning("announce_davey_missing: davey isn't installed, !announce will fail to connect")
        if not _opus_loaded():
            logging.warning("announce_opus_missing: libopus isn't installed, !announce will fail at playback")
        if not ffmpeg_available():
            logging.warning("announce_ffmpeg_missing: ffmpeg isn't installed, !announce can't play attached audio files")
        if not ytdlp_available():
            logging.warning("announce_ytdlp_missing: yt-dlp isn't installed, !announce can't play YouTube links")
        else:
            self.update_ytdlp_daily.start()

    async def cog_unload(self):
        self.update_ytdlp_daily.cancel()

    # YouTube changes break old yt-dlp within weeks; the binary updates
    # itself (see CLAUDE.md: Voice announcements). Daily here, and once more
    # after a failed resolve in _fetch_youtube.
    @tasks.loop(hours=24)
    async def update_ytdlp_daily(self):
        await self._update_ytdlp("daily")

    @update_ytdlp_daily.before_loop
    async def _before_update(self):
        await self.bot.wait_until_ready()

    async def _update_ytdlp(self, reason: str) -> bool:
        """Run `yt-dlp -U`; True if it ran (whatever it found)."""
        self._ytdlp_update_at = time.monotonic()
        try:
            result = await update_ytdlp()
        except DecodeError as e:
            logging.warning("ytdlp_update_failed reason=%s: %s", reason, e)
            return False
        logging.info("ytdlp_update reason=%s: %s", reason, result)
        return True

    async def _fetch_youtube(self, url: str):
        """`fetch_youtube`, with one `yt-dlp -U` and retry when the resolve
        fails — the usual cause is a YouTube change the running yt-dlp
        predates. Rate-limited so a bad link can't hammer GitHub."""
        try:
            return await fetch_youtube(url, max_secs=ANNOUNCE_MAX_SECS)
        except DecodeError as e:
            if "live stream" in str(e) or time.monotonic() - self._ytdlp_update_at < YTDLP_UPDATE_RETRY_SECS:
                raise
            logging.info("ytdlp_resolve_failed url=%s: %s", url, e)
            if not await self._update_ytdlp("resolve failed"):
                raise
            return await fetch_youtube(url, max_secs=ANNOUNCE_MAX_SECS)

    def _refusal(self, ctx: commands.Context) -> "discord.Embed | None":
        if self.tts is None:
            return emb("📢 Announce", "Voice announcements aren't configured (set `WYOMING_TTS_HOST`).", C_GREY)
        if ctx.guild is None:
            return emb("📢 Announce", "Announcements are read in a server's voice channel, not in DMs.", C_GREY)
        return None

    @commands.command(name="announce",
                      help="Read a message aloud in a voice channel (text-to-speech), or play an attached audio file "
                           "or a YouTube link there; x<N> repeats it, `stop` cuts it short",
                      usage='"<message>" <voice-channel> [x<N>]')
    async def cmd_announce(self, ctx: commands.Context, *, args: str = None):
        refusal = self._refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return

        if (args or "").strip().lower() == "stop":
            await self._stop_playing(ctx)
            return
        attachments = getattr(ctx.message, "attachments", None) or []
        attachment = next((a for a in attachments if is_audio_attachment(a)), None)
        url, args = youtube_url_in(args or "")
        text, query, repeats = parse_announce_args(args)
        if repeats is not None and not 1 <= repeats <= ANNOUNCE_MAX_REPEATS:
            await ctx.send(embed=emb("❌ Announce", f"Repeat between 1 and {ANNOUNCE_MAX_REPEATS} times (`x{ANNOUNCE_MAX_REPEATS}` at most).", C_RED))
            return
        repeats = repeats or 1
        media = attachment is not None or url is not None
        if media:
            if text or (attachment is not None and url is not None):
                await ctx.send(embed=emb("❌ Announce", "One thing to play: a message, an attached file or a YouTube link.", C_RED))
                return
            if attachment is not None and attachment.size > ANNOUNCE_MAX_FILE_BYTES:
                await ctx.send(embed=emb("❌ Too Big", f"Keep the file under {ANNOUNCE_MAX_FILE_BYTES // (1024 * 1024)} MB.", C_RED))
                return
        channel = None
        if query and (text or media):
            # What to play and a channel both given: a channel miss opens the pickers.
            try:
                channel = await VoiceChannelConverter().convert(ctx, query)
            except commands.BadArgument as exc:
                if not getattr(exc, "handled", False):  # a declined picker already answered
                    await ctx.send(embed=emb("❌ Channel Not Found",
                                             f"I couldn't find a voice channel matching **{query}**.", C_RED))
                return
        elif query:
            # No quotes: a lone argument is the channel if it clearly names
            # one, otherwise the message — without dropdowns either way.
            try:
                channel = await VoiceChannelConverter(prompt=False).convert(ctx, query)
            except commands.BadArgument:
                text = query

        if (not text and not media) or channel is None:
            if attachment is not None:
                description = f"Pick the voice channel to play **{attachment.filename}** in."
            elif url is not None:
                description = "Pick the voice channel to play the video's audio in."
            else:
                description = ("Pick the voice channel and write what the bot should say.\n"
                               "Usage: `!announce \"<message>\" <voice-channel>`, or attach an audio file "
                               "or paste a YouTube link instead of the message")
            values = await open_form(
                ctx, title="📢 Announce", description=description,
                fields=self._form_fields(ctx, channel, text, with_text=not media),
                button="Play it…" if media else "Write it…",
            )
            if values is None:
                return
            text = (values.get("text") or "").strip() or text
            ids = values.get("channel") or []
            if ids:
                channel = ctx.guild.get_channel(int(ids[0]))
            if channel is None:
                await ctx.send(embed=emb("❌ Announce", "Pick a voice channel to speak in.", C_RED))
                return
            if not text and not media:
                await ctx.send(embed=emb("❌ Announce", "Write the message to read out.", C_RED))
                return
        await self._announce(ctx, channel, text, attachment, url=url, repeats=repeats)

    @staticmethod
    def _form_fields(ctx, channel, text: str, *, with_text: bool = True) -> list:
        here = getattr(getattr(ctx.author, "voice", None), "channel", None)
        preset = channel or here  # the invoker's own channel is the likely target
        fields = [
            Field("channel", "Voice channel", kind="voice_channels",
                  defaults=(str(preset.id),) if preset is not None else ()),
        ]
        if with_text:
            fields.append(Field("text", "Message", kind="paragraph", max_length=ANNOUNCE_MAX_CHARS, default=text or None))
        return fields

    async def _announce(self, ctx: commands.Context, channel, text: str, attachment=None, *,
                        url: "str | None" = None, repeats: int = 1) -> None:
        media = attachment is not None or url is not None
        if not media and len(text) > ANNOUNCE_MAX_CHARS:
            await ctx.send(embed=emb("❌ Too Long", f"Keep it under {ANNOUNCE_MAX_CHARS} characters.", C_RED))
            return
        if not media and len(text) * repeats >= ANNOUNCE_MAX_TOTAL_CHARS:
            most = max(1, (ANNOUNCE_MAX_TOTAL_CHARS - 1) // max(len(text), 1))
            await ctx.send(embed=emb("❌ Too Long",
                                     f"The message times the repeats must stay under {ANNOUNCE_MAX_TOTAL_CHARS} characters — "
                                     f"this one can repeat {most} time{'s' if most != 1 else ''} at most.", C_RED))
            return
        if getattr(channel, "type", None) != discord.ChannelType.voice:
            await ctx.send(embed=emb("❌ Announce", f"**{channel.name}** isn't a voice channel (stages can't be announced in).", C_RED))
            return
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.connect and perms.speak):
            await ctx.send(embed=emb("❌ No Permission", f"I can't connect and speak in **{channel.name}**.", C_RED))
            return
        if not any(not m.bot for m in channel.members):
            await ctx.send(embed=emb("📢 Announce", f"Nobody is in **{channel.name}** to hear it.", C_GREY))
            return
        gid = ctx.guild.id
        if gid in self._busy:
            await ctx.send(embed=emb("📢 Announce", "An announcement is already playing in this server — wait for it, or `!announce stop`.", C_GREY))
            return
        self._busy.add(gid)
        try:
            # Synthesize (or decode) before connecting: the slow, failure-prone
            # step shouldn't happen with the bot sitting silent in the channel.
            if attachment is not None:
                try:
                    data = await attachment.read()
                except discord.HTTPException as e:
                    await ctx.send(embed=emb("❌ Audio File", f"Couldn't download **{attachment.filename}**: {e}", C_RED))
                    return
                try:
                    pcm = await decode_audio(data, max_secs=ANNOUNCE_MAX_SECS, filename=attachment.filename)
                except DecodeError as e:
                    await ctx.send(embed=emb("❌ Audio File", f"Couldn't play **{attachment.filename}**: {e}", C_RED))
                    return
                label = attachment.filename
            elif url is not None:
                try:
                    pcm, label = await self._fetch_youtube(url)
                except DecodeError as e:
                    await ctx.send(embed=emb("❌ YouTube", f"Couldn't play that video: {e}", C_RED))
                    return
            else:
                try:
                    audio = await self.tts.synthesize(text)
                except TTSError as e:
                    await ctx.send(embed=emb("❌ Text-to-Speech", f"Couldn't synthesize the message: {e}", C_RED))
                    return
                pcm = audio.to_discord()
            try:
                vc = await self._connect(ctx.guild, channel)
            except (asyncio.TimeoutError, discord.DiscordException, RuntimeError) as e:
                # RuntimeError is discord.py's "PyNaCl / davey library needed" complaint.
                await ctx.send(embed=emb("❌ Voice", f"Couldn't join **{channel.name}**: {e or 'timed out'}", C_RED))
                return
            played = 0
            try:
                self._playing[gid] = vc
                for _ in range(repeats):
                    if gid in self._stop:
                        break
                    await self._play(vc, pcm)
                    played += 1
            except discord.opus.OpusNotLoaded:
                await ctx.send(embed=emb("❌ Voice", "libopus isn't installed, so I can't encode audio.", C_RED))
                return
            finally:
                self._playing.pop(gid, None)
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    logging.warning("announce_disconnect_failed guild=%s: %s", gid, e)
            times = ""
            if gid in self._stop:
                times = f", stopped after {played} of {repeats}"
            elif repeats > 1:
                times = f", {repeats} times"
            if media:
                seconds = len(pcm) / (DISCORD_RATE * DISCORD_CHANNELS * SAMPLE_WIDTH)
                # The decoder cuts at the cap, so a clip that long was (almost surely) longer.
                cut = f" — the first {ANNOUNCE_MAX_SECS} seconds of it" if seconds >= ANNOUNCE_MAX_SECS - 0.5 else ""
                done = f"Played **{label}** in **{channel.name}**{cut}{times}."
            else:
                done = f"Read out in **{channel.name}**{times}:\n> {text}"
            await ctx.send(embed=emb("📢 Announced", done, C_GREEN))
        finally:
            self._busy.discard(gid)
            self._stop.discard(gid)

    async def _stop_playing(self, ctx: commands.Context) -> None:
        gid = ctx.guild.id
        if gid not in self._busy:
            await ctx.send(embed=emb("📢 Announce", "Nothing is playing in this server.", C_GREY))
            return
        # The flag ends the repeat loop; stopping the player ends the clip
        # under way (its `after` callback releases _play's wait).
        self._stop.add(gid)
        vc = self._playing.get(gid)
        if vc is not None:
            vc.stop()
        await ctx.send(embed=emb("⏹️ Announce", "Stopping.", C_GREY))

    @staticmethod
    async def _connect(guild, channel):
        # A connection left over from a crash makes connect() raise "already connected".
        leftover = guild.voice_client
        if leftover is not None:
            await leftover.disconnect(force=True)
        return await channel.connect(timeout=CONNECT_TIMEOUT, self_deaf=True)

    @staticmethod
    async def _play(vc, pcm: bytes) -> None:
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def _after(err):  # runs on discord.py's player thread
            loop.call_soon_threadsafe(lambda: done.done() or done.set_result(err))

        vc.play(discord.PCMAudio(io.BytesIO(pcm)), after=_after)
        seconds = len(pcm) / (DISCORD_RATE * DISCORD_CHANNELS * SAMPLE_WIDTH)
        try:
            err = await asyncio.wait_for(done, timeout=seconds + PLAY_GRACE_SECS)
        except asyncio.TimeoutError:
            vc.stop()
            logging.warning("announce_play_timeout after %.1fs", seconds + PLAY_GRACE_SECS)
            return
        if err is not None:
            logging.warning("announce_play_error: %s", err)


async def setup(bot):
    await bot.add_cog(AnnounceCog(bot))
