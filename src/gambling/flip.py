import random

from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, parse_amount, shop_charge,
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
    async def cmd_flip(self, ctx: commands.Context, amount: str = None):
        if await check_game_channel(ctx, "Gambling"):
            return
        uid = ctx.author.id
        if amount is None:
            await ctx.send("Usage: `!flip <amount>`")
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        if not await shop_charge(ctx, uid, amount):
            return
        if uid in state.rigged_flips:
            win = True
            state.rigged_flips[uid] -= 1
            if state.rigged_flips[uid] <= 0:
                del state.rigged_flips[uid]
            await save_rigged_flips()
        else:
            win = random.random() < 0.5
        if win:
            winnings = amount * 2
            gid = ctx.guild.id if ctx.guild else None
            await add_balance(uid, winnings, guild_id=gid, holder_name=ctx.author.display_name)
            if uid not in state.godmode_users:
                await record_gambling_event(uid, gained=amount)  # net: paid `amount`, received `2*amount`
            await try_set_record(gid, "flip", winnings, uid, ctx.author.display_name)
            await ctx.send(embed=emb("🪙 Heads!", f"**{ctx.author.display_name}** won **{amount:,} 🪙**! Balance: {await get_balance(uid):,} 🪙", C_GREEN))
        else:
            if uid not in state.godmode_users:
                await record_gambling_event(uid, lost=amount)
            await ctx.send(embed=emb("🪙 Tails!", f"**{ctx.author.display_name}** lost **{amount:,} 🪙**. Balance: {await get_balance(uid):,} 🪙", C_RED))


    # Mini Cactpot payout table
    CACTPOT_PAYOUTS = {
        6: 10000, 7: 36, 8: 720, 9: 360, 10: 80, 11: 252, 12: 108, 13: 72, 14: 54, 15: 180,
        16: 72, 17: 180, 18: 119, 19: 36, 20: 306, 21: 1080, 22: 144, 23: 1800, 24: 3600
    }


async def setup(bot):
    await bot.add_cog(FlipCog(bot))
