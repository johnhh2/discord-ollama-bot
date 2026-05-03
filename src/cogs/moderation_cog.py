import asyncio
import time

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_RED, C_GOLD, C_GREY,
    send_ephemeral,
)
from src.permissions import (
    check_command_permission,
)
from src.persistence import (
    load_saved_quotes,
)
from src import state



class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="audit")
    async def cmd_audit(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        if not state.audit_log:
            await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "No failed attempts recorded.", C_GREY))
            return
        recent = list(state.audit_log)[-5:]
        lines = []
        for e in reversed(recent):
            ts = time.strftime("%H:%M:%S", time.localtime(e["time"]))
            lines.append(f"**{ts}** — {e['user']}\n`{e['command']}`\n_{e['error']}_")
        await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "\n\n".join(lines), C_GOLD))


    @commands.command(name="clearbot")
    async def cmd_clear(self, ctx: commands.Context, n: int = 50):
        if not await check_command_permission(ctx):
            return
        deleted = 0
        async for message in ctx.channel.history(limit=500):
            if deleted >= n:
                break
            if message.author == self.bot.user:
                await message.delete()
                deleted += 1
        confirm = await ctx.send(embed=emb(
            "🗑️ Cleared",
            f"Deleted {deleted} bot message{'s' if deleted != 1 else ''}.",
            C_GREY,
        ))
        await asyncio.sleep(5)
        await confirm.delete()


    @commands.command(name="clearall", aliases=["clerall"])
    async def cmd_clearall(self, ctx: commands.Context, n: str = None):
        if not await check_command_permission(ctx):
            return

        if n is None:
            await ctx.send(embed=emb("❌ Missing Argument", "Usage: `!clearall <n>` — Delete last n messages", C_RED))
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
            confirm = await ctx.send(embed=emb(
                "🗑️ Cleared",
                f"Deleted {len(messages)-1} message{'s' if len(messages) != 1 else ''}.",
                C_GREY,
            ))
            await asyncio.sleep(5)
            await confirm.delete()
        except discord.Forbidden:
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete messages.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to delete messages: {str(e)}", C_RED))


    @commands.command(name="saved", aliases=["persistent", "saves"])
    async def cmd_saved(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        embed = discord.Embed(title="💾 Saved Data", color=C_GOLD)

        # Insurance data
        embed.add_field(
            name="🛡️ Insurance",
            value=f"**{len(state.insurance)}** users with active state.insurance",
            inline=False
        )

        # Mock data
        embed.add_field(
            name="🎭 Mock",
            value=f"**{len(state.active_mocks)}** users being mocked",
            inline=False
        )

        # Ragebait data
        embed.add_field(
            name="🎯 Ragebait",
            value=f"**{len(state.active_ragebaits)}** users with ragebait active",
            inline=False
        )

        # Tax data
        embed.add_field(
            name="🍆 Tax",
            value=f"**{len(state.active_taxes)}** users with active tax",
            inline=False
        )

        # Curse data
        embed.add_field(
            name="🔮 Curse",
            value=f"**{len(state.active_curses)}** users with curse active",
            inline=False
        )

        # Godmode users
        embed.add_field(
            name="👑 Godmode",
            value=f"**{len(state.godmode_users)}** users with godmode",
            inline=False
        )

        # Slot jackpot
        embed.add_field(
            name="💰 Slot Jackpot",
            value=f"**{state.slot_jackpot:,} 🪙** in jackpot",
            inline=False
        )

        # Chess games
        embed.add_field(
            name="♟️ Chess Games",
            value=f"**{len(state.active_chess_games)}** active correspondence chess games",
            inline=False
        )

        # Quote log
        _all_saved = await load_saved_quotes()
        _guild_id = str(ctx.guild.id) if ctx.guild else "dm"
        saved_quotes_count = len(_all_saved.get(_guild_id, []))
        embed.add_field(
            name="📜 Quotes",
            value=f"**{saved_quotes_count}** saved quotes (this server) | **{len(state.quote_log)}** in searchquote log (max 10)",
            inline=False
        )

        # Economy stats
        total_users = len(state.economy.get("users", {}))
        total_balance = sum(u.get("balance", 0) for u in state.economy.get("users", {}).values())
        embed.add_field(
            name="🪙 Economy",
            value=f"**{total_users}** users with **{total_balance:,} 🪙** total balance",
            inline=False
        )

        await send_ephemeral(ctx, embed=embed)



async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
