"""!announce: read a message aloud in a voice channel.

The text goes to a Wyoming text-to-speech server (src/tts.py), the PCM comes
back, and the bot joins the channel, plays it and leaves. Off until
WYOMING_TTS_HOST is set. Rules in CLAUDE.md: Voice announcements.
"""
import asyncio
import io
import logging
import re

import discord
from discord.ext import commands

from src.config import WYOMING_TTS_HOST, WYOMING_TTS_PORT, WYOMING_TTS_VOICE, ANNOUNCE_MAX_CHARS
from src.helpers import emb, VoiceChannelConverter, C_GREEN, C_RED, C_GREY
from src.settings_views import Field, open_form
from src.tts import WyomingTTS, TTSError, DISCORD_RATE, DISCORD_CHANNELS, SAMPLE_WIDTH

CONNECT_TIMEOUT = 10.0
PLAY_GRACE_SECS = 10.0  # allowed on top of the clip's own length before the player is given up on

# Straight or curly quotes — phones autocorrect to the latter.
_QUOTED_RE = re.compile(r'"([^"]*)"|“([^”]*)”')


def parse_announce_args(raw: str) -> tuple[str, str]:
    """`(message, channel_query)` from the typed arguments. The quoted part is
    the message, in either order; everything outside the quotes is the
    channel. Without quotes the message is empty and the whole argument comes
    back as the channel query, for the caller to try as one."""
    m = _QUOTED_RE.search(raw)
    if m is None:
        return "", raw.strip()
    text = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
    rest = (raw[:m.start()] + " " + raw[m.end():]).strip()
    return text, rest


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

    def _refusal(self, ctx: commands.Context) -> "discord.Embed | None":
        if self.tts is None:
            return emb("📢 Announce", "Voice announcements aren't configured (set `WYOMING_TTS_HOST`).", C_GREY)
        if ctx.guild is None:
            return emb("📢 Announce", "Announcements are read in a server's voice channel, not in DMs.", C_GREY)
        return None

    @commands.command(name="announce",
                      help="Read a message aloud in a voice channel (text-to-speech)",
                      usage='"<message>" <voice-channel>')
    async def cmd_announce(self, ctx: commands.Context, *, args: str = None):
        refusal = self._refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return

        text, query = parse_announce_args(args or "")
        channel = None
        if query and text:
            # Message and channel both given: a channel miss opens the pickers.
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

        if not text or channel is None:
            values = await open_form(
                ctx, title="📢 Announce",
                description="Pick the voice channel and write what the bot should say.\n"
                            "Usage: `!announce \"<message>\" <voice-channel>`",
                fields=self._form_fields(ctx, channel, text), button="Write it…",
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
            if not text:
                await ctx.send(embed=emb("❌ Announce", "Write the message to read out.", C_RED))
                return
        await self._announce(ctx, channel, text)

    @staticmethod
    def _form_fields(ctx, channel, text: str) -> list:
        here = getattr(getattr(ctx.author, "voice", None), "channel", None)
        preset = channel or here  # the invoker's own channel is the likely target
        return [
            Field("channel", "Voice channel", kind="voice_channels",
                  defaults=(str(preset.id),) if preset is not None else ()),
            Field("text", "Message", kind="paragraph", max_length=ANNOUNCE_MAX_CHARS, default=text or None),
        ]

    async def _announce(self, ctx: commands.Context, channel, text: str) -> None:
        if len(text) > ANNOUNCE_MAX_CHARS:
            await ctx.send(embed=emb("❌ Too Long", f"Keep it under {ANNOUNCE_MAX_CHARS} characters.", C_RED))
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
            await ctx.send(embed=emb("📢 Announce", "An announcement is already playing in this server — try again in a moment.", C_GREY))
            return
        self._busy.add(gid)
        try:
            # Synthesize before connecting: the slow, failure-prone step
            # shouldn't happen with the bot sitting silent in the channel.
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
            try:
                await self._play(vc, pcm)
            except discord.opus.OpusNotLoaded:
                await ctx.send(embed=emb("❌ Voice", "libopus isn't installed, so I can't encode audio.", C_RED))
                return
            finally:
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    logging.warning("announce_disconnect_failed guild=%s: %s", gid, e)
            await ctx.send(embed=emb("📢 Announced", f"Read out in **{channel.name}**:\n> {text}", C_GREEN))
        finally:
            self._busy.discard(gid)

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
