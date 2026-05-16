import logging
import time

import discord
from discord.ext import commands

from src.helpers import emb, C_GREEN, C_RED, C_GREY, C_GOLD
from src.permissions import requires_perm
from src.persistence import (
    save_voice_ping,
    delete_voice_ping,
    update_voice_ping_last_pinged,
)
from src import state


PING_COOLDOWN_SECS = 30 * 60


def _count_humans(channel: discord.VoiceChannel) -> int:
    return sum(1 for m in channel.members if not m.bot)


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="subscribe", aliases=["ping"])
    @requires_perm
    async def cmd_subscribe(
        self,
        ctx: commands.Context,
        *,
        channel: discord.VoiceChannel = None,
    ):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "Use this command in a server.", C_RED))
            return

        if channel is None:
            # List the caller's active subscriptions in this guild.
            mine = [
                (cid, data) for (cid, uid), data in state.voice_pings.items()
                if uid == ctx.author.id and data.get("guild_id") == ctx.guild.id
            ]
            if not mine:
                await ctx.send(embed=emb(
                    "🔔 Voice Pings",
                    "You aren't subscribed to any voice channels in this server.\n"
                    "Use `!subscribe <voice-channel>` to subscribe.",
                    C_GREY,
                ))
                return
            lines = []
            for cid, _ in mine:
                ch = ctx.guild.get_channel(cid)
                lines.append(f"• {ch.mention if ch else f'`#{cid}` (deleted)'}")
            await ctx.send(embed=emb(
                "🔔 Voice Pings",
                "You'll be DMed when these channels go from empty to active:\n"
                + "\n".join(lines)
                + "\n\nRun `!subscribe <voice-channel>` again to unsubscribe.",
                C_GOLD,
            ))
            return

        if channel.guild.id != ctx.guild.id:
            await ctx.send(embed=emb(
                "❌ Wrong Server",
                "That voice channel isn't in this server.",
                C_RED,
            ))
            return

        key = (channel.id, ctx.author.id)
        if key in state.voice_pings:
            del state.voice_pings[key]
            await delete_voice_ping(channel.id, ctx.author.id)
            await ctx.send(embed=emb(
                "🔕 Unsubscribed",
                f"You'll no longer be DMed when **{channel.name}** fills up.",
                C_GREY,
            ))
            return

        state.voice_pings[key] = {"guild_id": ctx.guild.id, "last_pinged_at": None}
        await save_voice_ping(ctx.guild.id, channel.id, ctx.author.id)
        await ctx.send(embed=emb(
            "🔔 Subscribed",
            f"I'll DM you when **{channel.name}** goes from empty to active "
            f"(at most once every {PING_COOLDOWN_SECS // 60} minutes).\n"
            "Run `!subscribe` to see your subscriptions, or `!subscribe #channel` again to unsubscribe.",
            C_GREEN,
        ))

    @cmd_subscribe.error
    async def cmd_subscribe_error(self, ctx, error):
        if isinstance(error, commands.ChannelNotFound):
            await ctx.send(embed=emb(
                "❌ Channel Not Found",
                "I couldn't find that voice channel. Try the channel ID, name, or `#mention`.",
                C_RED,
            ))
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(embed=emb(
                "❌ Invalid Channel",
                "That doesn't look like a voice channel.",
                C_RED,
            ))
            return
        raise error

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # Only care about joins (someone entered a new channel).
        if after.channel is None or after.channel == before.channel:
            return
        # Bots joining/leaving never trigger pings — and shouldn't count toward
        # the empty→1 transition either, which _count_humans already enforces.
        if member.bot:
            return

        channel = after.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        humans = _count_humans(channel)
        # _count_humans includes the just-joined member; "empty before" means
        # they were the only human to arrive.
        if humans != 1:
            return

        subscribers = [
            (uid, data) for (cid, uid), data in state.voice_pings.items()
            if cid == channel.id
        ]
        if not subscribers:
            return

        now = int(time.time())
        for uid, data in subscribers:
            # Skip if the subscriber is the person who just joined — they
            # obviously know the channel filled up.
            if uid == member.id:
                continue
            last = data.get("last_pinged_at")
            if last is not None and now - last < PING_COOLDOWN_SECS:
                continue

            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except (discord.NotFound, discord.HTTPException):
                    continue

            try:
                await user.send(embed=emb(
                    "🔔 Voice Channel Active",
                    f"**{member.display_name}** just joined **{channel.name}** "
                    f"in **{channel.guild.name}**.",
                    C_GREEN,
                ))
            except discord.Forbidden:
                # User has DMs closed — keep the subscription, just skip this one.
                logging.info(
                    "voice_ping_dm_forbidden uid=%s channel_id=%s",
                    uid, channel.id,
                )
                continue
            except discord.HTTPException as e:
                logging.warning(
                    "voice_ping_dm_failed uid=%s channel_id=%s error=%s",
                    uid, channel.id, type(e).__name__,
                )
                continue

            data["last_pinged_at"] = now
            try:
                await update_voice_ping_last_pinged(channel.id, uid, now)
            except Exception as e:
                logging.error(
                    "voice_ping_persist_failed uid=%s channel_id=%s error=%s",
                    uid, channel.id, type(e).__name__,
                )


async def setup(bot):
    await bot.add_cog(VoiceCog(bot))
