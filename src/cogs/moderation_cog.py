import asyncio
import time

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
            ts = time.strftime("%H:%M:%S", time.localtime(e["time"]))
            lines.append(f"**{ts}** — {e['user']}\n`{e['command']}`\n_{e['error']}_")
        await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "\n\n".join(lines), C_GOLD))


    @commands.command(name="clear", aliases=["clearall", "clerall"])
    @requires_perm
    async def cmd_clearall(self, ctx: commands.Context, n: str = None):

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

        try:
            messages = []
            async for message in ctx.channel.history(limit=n):
                messages.append(message)

            if not messages:
                await ctx.send(embed=emb("❌ No Messages", "No messages found to delete.", C_RED))
                return

            await ctx.channel.delete_messages(messages)
        except discord.Forbidden:
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete messages.", C_RED))
            return

        confirm = await ctx.send(embed=emb(
            "🗑️ Cleared",
            f"Deleted {len(messages)-1} message{'s' if len(messages) != 1 else ''}.",
            C_GREY,
        ))
        await asyncio.sleep(5)
        try:
            await confirm.delete()
        except discord.NotFound:
            pass


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
