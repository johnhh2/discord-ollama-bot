import random

from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, parse_amount, shop_charge, shop_payout, announce_record,
)
from src.economy import (
    get_balance, record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import (
    save_rigged_flips,
    try_set_record,
)
from src.dailies import keep_in_dailies_channel
from src import state


async def play_flip(author, channel, guild, amount: int, n: int = 1, side: str = "heads",
                    record_exclude: int = 0):
    """Charge amount×n and flip n coins on `side`, announcing in `channel`.

    Extracted from cmd_flip so the dailies-channel reaction claim can flip a
    player's claim (daily reward + scratchoff winnings) without a
    commands.Context. Inputs are
    assumed validated (amount >= 1, n >= 1, side in heads/tails).

    `record_exclude` shrinks the stake considered for the "biggest flip win"
    record (payouts are untouched). The dailies claim passes its property
    revenue portion here so property owners' auto-staked income can't
    trivialize the record; a hand-typed !flip wagers real coins knowingly and
    keeps the default 0.
    """
    uid = author.id
    total_cost = amount * n
    # shop_charge only uses ctx.send, so the channel satisfies it.
    if not await shop_charge(channel, uid, total_cost):
        return

    heads = 0
    rigged_used = 0
    for _ in range(n):
        if uid in state.rigged_flips:
            # Rigged flips force a win for whichever side the player picked.
            face = side
            state.rigged_flips[uid] -= 1
            if state.rigged_flips[uid] <= 0:
                del state.rigged_flips[uid]
            rigged_used += 1
        else:
            face = "heads" if random.random() < 0.5 else "tails"
        if face == "heads":
            heads += 1
    if rigged_used:
        await save_rigged_flips()

    tails = n - heads
    wins = heads if side == "heads" else tails
    winnings_per = amount * 2
    total_winnings = wins * winnings_per
    net = total_winnings - total_cost  # signed net P/L

    gid = guild.id if guild else None
    new_bal_record = False
    if total_winnings:
        new_bal_record = await shop_payout(uid, total_winnings, guild_id=gid, holder_name=author.display_name)
    if uid not in state.godmode_users:
        if net >= 0:
            await record_gambling_event(gid, uid, gained=net)
        else:
            await record_gambling_event(gid, uid, lost=-net)
    new_flip_record = False
    record_winnings_per = max(0, amount - record_exclude) * 2
    if wins and record_winnings_per > 0:
        new_flip_record = await try_set_record(gid, "flip", record_winnings_per, uid, author.display_name)
    new_bal = await get_balance(uid)

    if n == 1:
        face = "Heads" if heads == 1 else "Tails"
        if wins:
            msg = await channel.send(embed=emb(f"🪙 {face}!", f"**{author.display_name}** won **{amount:,} 🪙**! Balance: {new_bal:,} 🪙", C_GREEN))
        else:
            msg = await channel.send(embed=emb(f"🪙 {face}!", f"**{author.display_name}** lost **{amount:,} 🪙**. Balance: {new_bal:,} 🪙", C_RED))
    else:
        color = C_GREEN if net >= 0 else C_RED
        sign = "+" if net >= 0 else "-"
        title = f"🪙 Flipped {n} coins ({side})"
        desc = (
            f"**{author.display_name}** — {heads} heads / {tails} tails\n"
            f"Wins: **{wins}/{n}** • Net: **{sign}{abs(net):,} 🪙** • Balance: {new_bal:,} 🪙"
        )
        msg = await channel.send(embed=emb(title, desc, color))

    # Big results in the dailies channel stay pinned until the 5am reset.
    await keep_in_dailies_channel(guild, channel, msg, net)

    if new_flip_record:
        await announce_record(channel, "flip", author.display_name, record_winnings_per, holder_id=author.id)
    if new_bal_record:
        await announce_record(channel, "highest_balance", author.display_name, new_bal, holder_id=author.id)


class FlipCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="flip", aliases=["coinflip"])
    async def cmd_flip(self, ctx: commands.Context, amount: str = None, n: int = 1, side: str = "heads"):
        if await check_game_channel(ctx, "Gambling"):
            return
        if amount is None:
            await ctx.send("Usage: `!flip <amount> [n] [heads|tails]`")
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        if n < 1:
            await ctx.send("`n` must be a positive whole number.")
            return
        if n > 100_000:
            await ctx.send("You can flip at most 100,000 coins at a time.")
            return
        side = side.lower()
        if side in ("h", "head"):
            side = "heads"
        elif side in ("t", "tail"):
            side = "tails"
        if side not in ("heads", "tails"):
            await ctx.send("Side must be `heads` or `tails`.")
            return
        await play_flip(ctx.author, ctx.channel, ctx.guild, amount, n, side)


async def setup(bot):
    await bot.add_cog(FlipCog(bot))
