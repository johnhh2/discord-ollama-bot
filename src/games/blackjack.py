import random

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, parse_amount, shop_charge,
)
from src.economy import (
    add_balance, get_balance,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import (
    try_set_record,
)
from src.config import (
    BLACKJACK_NATURAL_MULT,
)
from src import state


# ── Blackjack helpers ─────────────────────────────────────────────────────────

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def new_deck() -> list[dict]:
    deck = [{"rank": r, "suit": s} for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def draw_card(deck: list[dict]) -> dict:
    return deck.pop()


def hand_value(hand: list[dict]) -> int:
    total = 0
    aces = 0
    for card in hand:
        r = card["rank"]
        if r in ("J", "Q", "K"):
            total += 10
        elif r == "A":
            aces += 1
            total += 11
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_hand(hand: list[dict], hide_second: bool = False) -> str:
    if hide_second and len(hand) >= 2:
        return f"{hand[0]['rank']}{hand[0]['suit']}  🂠"
    return "  ".join(f"{c['rank']}{c['suit']}" for c in hand)


def build_blackjack_display(
    player: list[dict],
    dealer: list[dict],
    pval: int,
    hide_dealer: bool = False,
    dval: int = None,
    username: str = "You",
) -> str:
    dealer_str = format_hand(dealer, hide_second=hide_dealer)
    player_str = format_hand(player)
    dealer_label = "Dealer" if hide_dealer or dval is None else f"Dealer ({dval})"
    return f"**{dealer_label}:** {dealer_str}\n**{username} ({pval}):** {player_str}"


def dealer_play(deck: list, dealer: list) -> None:
    """Mutate `dealer` by drawing until its hand value is 17 or higher.

    Hits on soft/hard 16, stands on 17+. Used by `_blackjack_stand`; broken
    out so the dealer rule can be tested in isolation.
    """
    while hand_value(dealer) <= 16:
        dealer.append(draw_card(deck))


async def _blackjack_stand(message: discord.Message, uid: int, game: dict):
    dealer = game["dealer_hand"]
    player = game["player_hand"]
    deck = game["deck"]

    dealer_play(deck, dealer)

    pval = hand_value(player)
    dval = hand_value(dealer)
    amount = game["amount"]
    uid_name = message.author.display_name
    del state.active_blackjack_games[uid]

    display = build_blackjack_display(player, dealer, pval, hide_dealer=False, dval=dval, username=uid_name)

    if dval > 21 or pval > dval:
        gid = message.guild.id if message.guild else None
        await add_balance(uid, amount * 2, guild_id=gid, holder_name=uid_name)
        await try_set_record(gid, "blackjack", amount * 2, uid, uid_name,
                       player_hand=format_hand(player), player_score=pval,
                       dealer_score=dval)
        color, result = C_GREEN, f"✅ **{uid_name}** wins **{amount:,} 🪙**! Balance: {await get_balance(uid):,} 🪙"
    elif pval == dval:
        await add_balance(uid, amount)
        color, result = C_GOLD, f"🤝 Push! Bet returned. Balance: {await get_balance(uid):,} 🪙"
    else:
        color, result = C_RED, f"❌ Dealer wins. **{uid_name}** loses **{amount:,} 🪙**. Balance: {await get_balance(uid):,} 🪙"

    await message.channel.send(embed=emb("🃏 Blackjack", display + f"\n\n{result}", color))



class BlackjackCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="blackjack", aliases=["bj", "blackj"])
    async def cmd_blackjack(self, ctx: commands.Context, amount: str = None):
        if await check_game_channel(ctx, "Gambling"):
            return
        uid = ctx.author.id
        if amount is None:
            await ctx.send("Usage: `!blackjack <amount>`")
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        if uid in state.active_blackjack_games:
            await ctx.send(embed=emb("🃏 Already Playing", "Just type `hit` or `stand`.", C_GOLD))
            return
        if not await shop_charge(ctx, uid, amount):
            return
        deck = new_deck()
        player = [draw_card(deck), draw_card(deck)]
        dealer = [draw_card(deck), draw_card(deck)]
        pval = hand_value(player)
        dval = hand_value(dealer)

        state.active_blackjack_games[uid] = {
            "amount": amount,
            "player_hand": player,
            "dealer_hand": dealer,
            "deck": deck,
            "channel_id": ctx.channel.id,
        }

        username = ctx.author.display_name
        display = build_blackjack_display(player, dealer, pval, hide_dealer=True, username=username)

        # Natural blackjack
        if pval == 21:
            del state.active_blackjack_games[uid]
            full_display = build_blackjack_display(player, dealer, pval, hide_dealer=False, dval=dval, username=username)
            if dval == 21:
                await add_balance(uid, amount)
                await ctx.send(embed=emb("🃏 Blackjack — Push", full_display + "\n\nBoth have Blackjack! Bet returned.", C_GOLD))
            else:
                winnings = int(amount * BLACKJACK_NATURAL_MULT)
                gid = ctx.guild.id if ctx.guild else None
                await add_balance(uid, winnings, guild_id=gid, holder_name=username)
                await try_set_record(gid, "blackjack", winnings, uid, username,
                               player_hand=format_hand(player), player_score=pval,
                               dealer_score=dval)
                await ctx.send(embed=emb("🃏 Blackjack!", full_display + f"\n\n**{ctx.author.display_name}** wins **{winnings:,} 🪙**! Balance: {await get_balance(uid):,} 🪙", C_GREEN))
            return

        await ctx.send(embed=emb("🃏 Blackjack", display + "\n\nType `hit` to draw a card or `stand` to hold.", C_BLUE))



async def setup(bot):
    await bot.add_cog(BlackjackCog(bot))
