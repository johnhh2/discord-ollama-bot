import random

import discord
from discord.ext import commands

from src.command_menu import Action, Call, send_with_menu
from src.features import disabled_feature_for
from src.settings_views import Field

from src.helpers import (
    emb, C_GREEN, C_RED, parse_amount, shop_charge, shop_payout, announce_record,
)
from src.economy import (
    get_balance, get_total_balance, record_gambling_event,
    try_set_loss_record, format_loss_record_detail, GAMBLING_LOSS_RECORD, _ensure_user,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import (
    save_economy,
    save_rigged_flips,
    try_set_record,
)
from src.dailies import keep_in_dailies_channel
from src.gambling.play_again import PlayAgainView
from src import state


def default_side(uid: int) -> str:
    """The side `!flip default` set, heads until then."""
    return state.economy["users"].get(str(uid), {}).get("flip_side") or "heads"


def _parse_side(side: str) -> str | None:
    side = side.lower()
    if side in ("h", "head"):
        return "heads"
    if side in ("t", "tail"):
        return "tails"
    return side if side in ("heads", "tails") else None


async def play_flip(author, channel, guild, amount: int, n: int = 1, side: str = None,
                    record_exclude: int = 0, play_again: bool = False):
    """Charge amount×n and flip n coins on `side` (the player's default when
    None), announcing in `channel`.

    Takes no commands.Context, so the dailies-channel reaction claim can flip
    a player's claim (daily reward + scratchoff winnings). Inputs are assumed
    validated (amount >= 1, n >= 1, side in heads/tails).

    `play_again` attaches "Flip Again" (same stake) and "2x" (double the
    per-coin stake, same n and side) buttons to the result (`PlayAgainView`).
    `!flip` passes True; the dailies claim keeps the default so the daily
    stake stays a one-shot.

    `record_exclude` shrinks the stake considered for the "biggest flip payout"
    record (payouts are untouched). The dailies claim passes its property
    revenue portion here so property owners' auto-staked income can't
    trivialize the record; a hand-typed !flip wagers real coins knowingly and
    keeps the default 0.
    """
    uid = author.id
    side = side or default_side(uid)
    total_cost = amount * n
    # shop_charge only uses ctx.send, so the channel satisfies it.
    if not await shop_charge(channel, uid, total_cost):
        return

    # Built only after the charge lands so a declined bet never leaves an
    # orphan view (and its timeout task) behind.
    view = None
    if play_again:
        async def _replay(stake: int):
            await play_flip(author, channel, guild, stake, n, side, play_again=True)

        def _stake_label(per_coin: int) -> str:
            return f"{per_coin:,} 🪙" if n == 1 else f"{n} × {per_coin:,} 🪙"
        view = PlayAgainView(
            author, guild, replay=_replay,
            options=[(f"Flip Again · {_stake_label(amount)}", amount),
                     (f"2x · {_stake_label(amount * 2)}", amount * 2)],
            not_yours="Not your flip — run `!flip` for your own.",
        )
    send_kwargs = {"view": view} if view is not None else {}

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
    net = total_winnings - total_cost

    gid = guild.id if guild else None
    new_bal_record = False
    if total_winnings:
        new_bal_record = await shop_payout(uid, total_winnings, guild_id=gid, holder_name=author.display_name)
    if uid not in state.godmode_users:
        if net >= 0:
            await record_gambling_event(gid, uid, gained=net, channel_id=channel.id)
        else:
            await record_gambling_event(gid, uid, lost=-net, channel_id=channel.id)
    new_flip_record = False
    record_winnings_per = max(0, amount - record_exclude) * 2
    if wins and record_winnings_per > 0:
        new_flip_record = await try_set_record(gid, "flip", record_winnings_per, uid, author.display_name)
    # Biggest gambling loss: the batch's net, on the record-eligible stake
    # (n - 2·wins coins' worth — the same number as -net for a plain !flip).
    record_bet = max(0, amount - record_exclude)
    record_loss = record_bet * (n - 2 * wins)
    loss_meta = {"game": "flip", "bet": record_bet, "coins": n}
    new_loss_record = False
    if net < 0:
        new_loss_record = await try_set_loss_record(gid, uid, author.display_name, record_loss, **loss_meta)
    new_bal = await get_balance(uid)

    if n == 1:
        face = "Heads" if heads == 1 else "Tails"
        if wins:
            msg = await channel.send(embed=emb(f"🪙 {face}!", f"**{author.display_name}** won **{amount:,} 🪙**! Balance: {new_bal:,} 🪙", C_GREEN), silent=True, **send_kwargs)
        else:
            msg = await channel.send(embed=emb(f"🪙 {face}!", f"**{author.display_name}** lost **{amount:,} 🪙**. Balance: {new_bal:,} 🪙", C_RED), silent=True, **send_kwargs)
    else:
        color = C_GREEN if net >= 0 else C_RED
        sign = "+" if net >= 0 else "-"
        title = f"🪙 Flipped {n} coins ({side})"
        desc = (
            f"**{author.display_name}** — {heads} heads / {tails} tails\n"
            f"Wins: **{wins}/{n}** • Net: **{sign}{abs(net):,} 🪙** • Balance: {new_bal:,} 🪙"
        )
        msg = await channel.send(embed=emb(title, desc, color), silent=True, **send_kwargs)
    if view is not None:
        view.message = msg  # on_timeout strips the buttons from it

    # Big results in the dailies channel stay pinned until the 5am reset.
    await keep_in_dailies_channel(guild, channel, msg, net)

    if new_flip_record:
        await announce_record(channel, "flip", author.display_name, record_winnings_per, holder_id=author.id)
    if new_bal_record:
        await announce_record(channel, "highest_balance", author.display_name, await get_total_balance(uid), holder_id=author.id)
    if new_loss_record:
        await announce_record(channel, GAMBLING_LOSS_RECORD, author.display_name, record_loss,
                              detail=format_loss_record_detail(loss_meta), holder_id=author.id)


class FlipCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="flip", aliases=["coinflip"], invoke_without_command=True,
                    help="Flip a coin; with an amount, bet coins at even money on up to 100,000 flips of heads or tails",
                    usage="[amount] [n] [heads|tails]")
    async def cmd_flip(self, ctx: commands.Context, amount: str = None, n: str = None, side: str = None):
        if amount is None:
            # No stake, so no gambling gate: this runs with gambling off (FREE_WHEN_BARE) and anywhere.
            await self._free_flip(ctx)
            return
        if await check_game_channel(ctx, "Gambling"):
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        # `!flip 50k tails`: a side in the count's slot means one coin.
        if side is None and n is not None and _parse_side(str(n)) is not None:
            n, side = 1, str(n)
        try:
            n = 1 if n is None else int(n)
        except ValueError:
            n = 0
        if n < 1:
            await ctx.send("`n` must be a positive whole number.")
            return
        if n > 100_000:
            await ctx.send("You can flip at most 100,000 coins at a time.")
            return
        if side is not None:
            side = _parse_side(side)
            if side is None:
                await ctx.send("Side must be `heads` or `tails`.")
                return
        await play_flip(ctx.author, ctx.channel, ctx.guild, amount, n, side, play_again=True)

    @cmd_flip.command(name="default",
                      help="Set the side your flips call when you don't name one, dailies flips included",
                      usage="[heads|tails]")
    async def cmd_flip_default(self, ctx: commands.Context, side: str = None):
        uid = ctx.author.id
        if side is None:
            await ctx.send(embed=emb("🪙 Flip Default", f"Your flips call **{default_side(uid)}**. "
                                     "Change it with `!flip default heads|tails`.", C_GREEN))
            return
        parsed = _parse_side(side)
        if parsed is None:
            await ctx.send("Side must be `heads` or `tails`.")
            return
        await _ensure_user(uid)
        state.economy["users"][str(uid)]["flip_side"] = None if parsed == "heads" else parsed
        await save_economy(uid=uid)
        await ctx.send(embed=emb("🪙 Flip Default", f"Your flips now call **{parsed}** unless you name a side.", C_GREEN))

    async def _free_flip(self, ctx: commands.Context) -> None:
        """Heads or tails, nothing staked. Where betting is open, a Bet coins
        button and the typed hint; where gambling is off, neither."""
        side = random.choice(("Heads", "Tails"))
        desc = f"**{ctx.author.display_name}** flipped a coin: **{side}**"
        gid = ctx.guild.id if ctx.guild else None
        betting = disabled_feature_for("flip", gid) is None
        actions = []
        if betting:
            desc += "\n\n-# `!flip <coins>` wagers on a flip instantly, at even money."
            actions.append(Action(
                label="Bet coins", emoji="🪙", command="flip", style=discord.ButtonStyle.primary, title="Bet on a flip",
                fields=(Field("amount", "Coins to bet", placeholder="500, 2.5k, half, all", max_length=12),
                        Field("side", "Call it", kind="choices", options=(("Heads", "heads"), ("Tails", "tails")),
                              defaults=(default_side(ctx.author.id),))),
                build=lambda v, guild: Call(args=(v["amount"], 1, v["side"][0]),
                                            content=f"!flip {v['amount']} 1 {v['side'][0]}"),
            ))
        await send_with_menu(self.bot, ctx, actions, embed=emb("🪙 Coin Flip", desc, C_GREEN))


async def setup(bot):
    await bot.add_cog(FlipCog(bot))
