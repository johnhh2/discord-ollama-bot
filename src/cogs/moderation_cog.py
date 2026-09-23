import asyncio
import datetime

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_RED, C_GOLD, C_GREY,
    send_ephemeral,
)
from src.permissions import (
    requires_perm,
)
from src import state

# Discord's "You can only bulk delete messages that are under 14 days old."
BULK_DELETE_TOO_OLD = 50034
# Well inside Discord's 14-day bulk limit, so its clock and ours can disagree.
_SAFE_BULK_AGE = datetime.timedelta(days=13)


async def _purge_near_cutoff(channel, limit: int) -> list:
    """Delete the last `limit` messages when a plain purge() hit 50034: bulk
    only what is safely young, single-delete the rest (slow, rate-limited)."""
    cutoff = discord.utils.utcnow() - _SAFE_BULK_AGE
    # oldest_first=False: `after` alone would flip history to oldest-first.
    deleted = await channel.purge(limit=limit, after=cutoff, oldest_first=False)
    if len(deleted) < limit:
        deleted += await channel.purge(limit=limit - len(deleted), bulk=False)
    return deleted


class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="audit",
                      help="Show the last 5 failed command attempts")
    @requires_perm
    async def cmd_audit(self, ctx: commands.Context):
        if not state.audit_log:
            await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "No failed attempts recorded.", C_GREY))
            return
        recent = list(state.audit_log)[-5:]
        lines = []
        for e in reversed(recent):
            # Discord renders <t:...:T> in the viewer's own timezone —
            # container-local time.strftime was UTC and misleading.
            lines.append(f"**<t:{int(e['time'])}:T>** — {e['user']}\n`{e['command']}`\n_{e['error']}_")
        await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "\n\n".join(lines), C_GOLD))


    @commands.command(name="clear", aliases=["clearall", "clerall"],
                      help="Delete the last n messages in this channel from any author (max 100)",
                      usage="<n>")
    @requires_perm
    async def cmd_clearall(self, ctx: commands.Context, n: str = None):

        if ctx.guild is None:
            # DMChannel has no purge(); without this a DM invocation raises
            # AttributeError into the global error handler.
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return
        if n is None:
            await ctx.send(embed=emb("❌ Missing Argument", "Usage: `!clear <n>` — Delete last n messages", C_RED))
            return

        try:
            n = int(n) + 1
            if n <= 1:
                await ctx.send(embed=emb("❌ Invalid Number", "Please provide a positive integer.", C_RED))
                return
            if n > 101:
                await ctx.send(embed=emb("❌ Too Many", "Maximum 100 messages at a time.", C_RED))
                return
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Input", "Please provide a valid number.", C_RED))
            return

        # purge() bulk-deletes messages <14 days old and single-deletes older
        # ones, but it draws that line on the local clock: a message right at
        # the boundary (or a skewed clock) still gets bulked, and Discord 400s
        # the whole batch with 50034, deleting nothing.
        try:
            try:
                deleted = await ctx.channel.purge(limit=n)
            except discord.HTTPException as e:
                if e.code != BULK_DELETE_TOO_OLD:
                    raise
                deleted = await _purge_near_cutoff(ctx.channel, n)
        except discord.Forbidden:
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete messages.", C_RED))
            return

        # purge() always eats the !clear command message itself, so the real
        # count is len(deleted) - 1 — pluralize and empty-check on that.
        n_deleted = max(0, len(deleted) - 1)
        if n_deleted == 0:
            await ctx.send(embed=emb("❌ No Messages", "No messages found to delete.", C_RED))
            return

        confirm = await ctx.send(embed=emb(
            "🗑️ Cleared",
            f"Deleted {n_deleted} message{'s' if n_deleted != 1 else ''}.",
            C_GREY,
        ))
        await asyncio.sleep(5)
        try:
            await confirm.delete()
        except discord.NotFound:
            pass  # the confirmation was already deleted during the 5 s wait


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
