import logging
import time

import discord
from discord.ext import commands

from src.helpers import emb, MemberConverter, C_GREEN, C_RED, C_GREY, C_GOLD
from src.permissions import requires_perm
from src.persistence import (
    save_voice_ping,
    delete_voice_ping,
    update_voice_ping_last_pinged,
    save_voice_ping_ignore,
    delete_voice_ping_ignore,
)
from src import state


PING_COOLDOWN_SECS = 30 * 60


def _count_humans(channel: discord.VoiceChannel) -> int:
    return sum(1 for m in channel.members if not m.bot)


def _is_first_relevant_arrival(channel, joiner, ignored) -> bool:
    """True if `joiner` is the only human in `channel` that a subscriber cares
    about — i.e. every other current member is a bot or is in `ignored`.

    This is the per-subscriber generalization of the old 0→1 rule: with an empty
    ignore list it reduces to "the joiner is the only human present". With an
    ignore list, ignored members (and bots) don't count, so a non-ignored person
    arriving as the 2nd/3rd member still counts as the first relevant arrival —
    as long as everyone already there was ignored or a bot.
    """
    for m in channel.members:
        if m.id == joiner.id:
            continue
        if m.bot:
            continue
        if m.id in ignored:
            continue
        return False  # someone the subscriber cares about was already here
    return True


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="subscribe", aliases=["ping"], invoke_without_command=True)
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

    @cmd_subscribe.command(name="ignore", aliases=["unignore"])
    @requires_perm
    async def cmd_subscribe_ignore(
        self,
        ctx: commands.Context,
        *,
        query: str = None,
    ):
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "Use this command in a server.", C_RED))
            return

        key = (ctx.guild.id, ctx.author.id)

        # Resolve the target ourselves (project MemberConverter supports
        # case-insensitive display-name / username substring matching, which the
        # raw `discord.Member` annotation doesn't). Resolving inline also lets us
        # tell "no argument → list" apart from "given but not found → error",
        # and keeps the not-found path from escaping as an unhandled
        # MemberNotFound that the global error logger would report.
        member = None
        if query is not None:
            try:
                member = await MemberConverter().convert(ctx, query.strip())
            except commands.BadArgument as exc:
                msg = str(exc)
                if "matched multiple members" in msg:
                    await ctx.send(embed=emb("❌ Ambiguous User", msg, C_RED))
                else:
                    await ctx.send(embed=emb(
                        "❌ User Not Found",
                        f"I couldn't find a member matching **{query.strip()}** in this server. "
                        "Try a `@mention`, user ID, or part of their name.",
                        C_RED,
                    ))
                return

        if member is None:
            # List the caller's ignored users in this guild.
            ignored = state.voice_ping_ignores.get(key, set())
            if not ignored:
                await ctx.send(embed=emb(
                    "🙈 Ignored Triggers",
                    "You aren't ignoring anyone for voice pings in this server.\n"
                    "Use `!subscribe ignore @user` to ignore someone — you won't be "
                    "pinged when *they* fill up a channel you're subscribed to, and it "
                    "won't burn your per-channel cooldown.",
                    C_GREY,
                ))
                return
            lines = []
            for uid in ignored:
                m = ctx.guild.get_member(uid)
                lines.append(f"• {m.mention if m else f'`{uid}` (left server)'}")
            await ctx.send(embed=emb(
                "🙈 Ignored Triggers",
                "You won't be pinged when these users fill up a channel you're "
                "subscribed to:\n" + "\n".join(lines)
                + "\n\nRun `!subscribe ignore @user` again to stop ignoring them.",
                C_GOLD,
            ))
            return

        if member.id == ctx.author.id:
            await ctx.send(embed=emb(
                "❌ Can't Ignore Yourself",
                "You're already never pinged for channels you fill up yourself.",
                C_RED,
            ))
            return
        if member.bot:
            await ctx.send(embed=emb(
                "❌ Bots Don't Trigger Pings",
                "Bots never count toward a channel filling up, so ignoring one does nothing.",
                C_RED,
            ))
            return

        ignored = state.voice_ping_ignores.get(key)
        if ignored and member.id in ignored:
            ignored.discard(member.id)
            if not ignored:
                state.voice_ping_ignores.pop(key, None)
            await delete_voice_ping_ignore(ctx.guild.id, ctx.author.id, member.id)
            await ctx.send(embed=emb(
                "🔔 No Longer Ignoring",
                f"You'll be pinged again when **{member.display_name}** fills up a "
                "channel you're subscribed to.",
                C_GREY,
            ))
            return

        state.voice_ping_ignores.setdefault(key, set()).add(member.id)
        await save_voice_ping_ignore(ctx.guild.id, ctx.author.id, member.id)
        await ctx.send(embed=emb(
            "🙈 Ignoring Triggers",
            f"You won't be pinged when **{member.display_name}** fills up a channel "
            "you're subscribed to, and it won't use up your per-channel cooldown.\n"
            "Run `!subscribe ignore @user` again to undo, or `!subscribe ignore` to see your list.",
            C_GREEN,
        ))

    @cmd_subscribe.error
    async def cmd_subscribe_error(self, ctx, error):
        # `ignore`/`unignore` resolve their target inside the command body, so a
        # bad user argument never surfaces here — only a bad voice-channel
        # argument to the base `!subscribe <channel>` does.
        if isinstance(error, commands.ChannelNotFound):
            await ctx.send(embed=emb(
                "❌ Channel Not Found",
                "I couldn't find that voice channel. Try the channel ID, name, or `#mention`.",
                C_RED,
            ))
            error.handled = True
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(embed=emb(
                "❌ Invalid Channel",
                "That doesn't look like a voice channel.",
                C_RED,
            ))
            error.handled = True
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

        # We don't gate on a global 0→1 transition here. Whether this join is a
        # "fill up" event is decided per-subscriber below, because each
        # subscriber's ignore list changes what counts as the first relevant
        # arrival (see _is_first_relevant_arrival).
        subscribers = [
            (uid, data) for (cid, uid), data in state.voice_pings.items()
            if cid == channel.id
        ]
        if not subscribers:
            return

        now = int(time.time())
        guild_id = channel.guild.id
        for uid, data in subscribers:
            # Skip if the subscriber is the person who just joined — they
            # obviously know the channel filled up.
            if uid == member.id:
                continue
            ignored = state.voice_ping_ignores.get((guild_id, uid), ())
            # Skip if this subscriber has ignored the triggering member. This
            # happens BEFORE the cooldown check and never touches last_pinged_at,
            # so an ignored trigger doesn't burn the cooldown — a later trigger
            # by a non-ignored member can still ping.
            if member.id in ignored:
                continue
            # Only ping when `member` is the FIRST human this subscriber cares
            # about to be in the channel. If a non-ignored human was already
            # present before this join, the subscriber was already notified for
            # them; don't ping again for every subsequent joiner.
            if not _is_first_relevant_arrival(channel, member, ignored):
                continue
            last = data.get("last_pinged_at")
            if last is not None and now - last < PING_COOLDOWN_SECS:
                continue

            # Claim the cooldown synchronously BEFORE the fetch/DM awaits —
            # a join/leave/rejoin inside the DM round-trip would otherwise
            # pass the gate twice and double-DM. Rolled back below on send
            # failure so a failed DM doesn't burn the cooldown.
            data["last_pinged_at"] = now

            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except (discord.NotFound, discord.HTTPException):
                    data["last_pinged_at"] = last
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
                data["last_pinged_at"] = last
                continue
            except discord.HTTPException as e:
                logging.warning(
                    "voice_ping_dm_failed uid=%s channel_id=%s error=%s",
                    uid, channel.id, type(e).__name__,
                )
                data["last_pinged_at"] = last
                continue

            try:
                await update_voice_ping_last_pinged(channel.id, uid, now)
            except Exception as e:
                logging.error(
                    "voice_ping_persist_failed uid=%s channel_id=%s error=%s",
                    uid, channel.id, type(e).__name__,
                )


async def setup(bot):
    await bot.add_cog(VoiceCog(bot))
