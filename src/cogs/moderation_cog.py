import asyncio

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



class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="audit")
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


    @commands.command(name="clear", aliases=["clearall", "clerall"])
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

        # purge() bulk-deletes messages <14 days old and falls back to
        # single deletes for older ones — bulk_delete (which is what
        # ctx.channel.delete_messages calls) 400s with error 50034 on
        # anything older than 14 days.
        try:
            deleted = await ctx.channel.purge(limit=n)
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
            pass


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
