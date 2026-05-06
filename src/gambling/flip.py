import random

from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, parse_amount, shop_charge, announce_record,
)
from src.economy import (
    add_balance, get_balance, record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import (
    save_rigged_flips,
    try_set_record,
)
from src import state



class FlipCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="flip", aliases=["coinflip"])
    async def cmd_flip(self, ctx: commands.Context, amount: str = None, n: int = 1):
        if await check_game_channel(ctx, "Gambling"):
            return
        uid = ctx.author.id
        if amount is None:
            await ctx.send("Usage: `!flip <amount> [n]`")
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        if n < 1:
            await ctx.send("`n` must be a positive whole number.")
            return
        if n > 100:
            await ctx.send("You can flip at most 100 coins at a time.")
            return
        total_cost = amount * n
        if not await shop_charge(ctx, uid, total_cost):
            return

        wins = 0
        rigged_used = 0
        for _ in range(n):
            if uid in state.rigged_flips:
                win = True
                state.rigged_flips[uid] -= 1
                if state.rigged_flips[uid] <= 0:
                    del state.rigged_flips[uid]
                rigged_used += 1
            else:
                win = random.random() < 0.5
            if win:
                wins += 1
        if rigged_used:
            await save_rigged_flips()

        losses = n - wins
        winnings_per = amount * 2
        total_winnings = wins * winnings_per
        net = total_winnings - total_cost  # signed net P/L

        gid = ctx.guild.id if ctx.guild else None
        new_bal_record = False
        if total_winnings:
            new_bal_record = await add_balance(uid, total_winnings, guild_id=gid, holder_name=ctx.author.display_name)
        if uid not in state.godmode_users:
            if net >= 0:
                await record_gambling_event(uid, gained=net)
            else:
                await record_gambling_event(uid, lost=-net)
        new_flip_record = False
        if wins:
            new_flip_record = await try_set_record(gid, "flip", winnings_per, uid, ctx.author.display_name)
        new_bal = await get_balance(uid)

        if n == 1:
            if wins:
                await ctx.send(embed=emb("🪙 Heads!", f"**{ctx.author.display_name}** won **{amount:,} 🪙**! Balance: {new_bal:,} 🪙", C_GREEN))
            else:
                await ctx.send(embed=emb("🪙 Tails!", f"**{ctx.author.display_name}** lost **{amount:,} 🪙**. Balance: {new_bal:,} 🪙", C_RED))
        else:
            color = C_GREEN if net >= 0 else C_RED
            sign = "+" if net >= 0 else "-"
            title = f"🪙 Flipped {n} coins"
            desc = (
                f"**{ctx.author.display_name}** — {wins} heads / {losses} tails\n"
                f"Net: **{sign}{abs(net):,} 🪙** • Balance: {new_bal:,} 🪙"
            )
            await ctx.send(embed=emb(title, desc, color))

        if new_flip_record:
            await announce_record(ctx.channel, "flip", ctx.author.display_name, winnings_per)
        if new_bal_record:
            await announce_record(ctx.channel, "highest_balance", ctx.author.display_name, new_bal)


    # Mini Cactpot payout table
    CACTPOT_PAYOUTS = {
        6: 10000, 7: 36, 8: 720, 9: 360, 10: 80, 11: 252, 12: 108, 13: 72, 14: 54, 15: 180,
        16: 72, 17: 180, 18: 119, 19: 36, 20: 306, 21: 1080, 22: 144, 23: 1800, 24: 3600
    }


async def setup(bot):
    await bot.add_cog(FlipCog(bot))
