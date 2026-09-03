import random

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, parse_amount, shop_charge, shop_payout, announce_record,
)
from src.economy import (
    get_balance, get_total_balance, record_gambling_event,
)
from src.permissions import (
    check_game_channel, is_silenced,
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

    Hits on soft/hard 16, stands on 17+. Used by `_stand`; broken out so
    the dealer rule can be tested in isolation.
    """
    while hand_value(dealer) <= 16:
        dealer.append(draw_card(deck))


# ── Hit / Stand buttons ───────────────────────────────────────────────────────

# How long a turn's Hit/Stand buttons stay clickable. Typing `hit` / `stand`
# keeps working after they vanish; the hand itself never expires.
BLACKJACK_BUTTON_TIMEOUT = 60.0

def can_double(game: dict) -> bool:
    """Double down is offered on the hand's first decision only: two cards,
    nothing drawn yet. A property of the hand rather than a turn counter, so
    it would carry over per-hand if split were ever added."""
    return len(game["player_hand"]) == 2


def _turn_prompt(game: dict) -> str:
    if can_double(game):
        return "Use the buttons below, or type `hit`, `stand`, or `double`."
    return "Use the buttons below, or type `hit` / `stand`."


def _doubled_note(game: dict) -> str:
    if not game.get("doubled"):
        return ""
    return f"\n⏫ Doubled down — stake **{game['amount']:,} 🪙**."


class BlackjackView(discord.ui.View):
    """Hit / Stand (/ Double Down) buttons under one turn of a player's hand.

    Same contract as `PlayAgainView` on !slots / !flip: only the player can
    click, the double-click guard flips before any await, every click
    consults `is_silenced`, and a click strips its buttons before the next
    turn (or the result) posts with its own set. The game dict tracks the
    current set in `game["view"]` so a typed `hit` / `stand`, `!stop`, or
    the next turn can retire buttons that are no longer current.
    """

    def __init__(self, author, channel, guild, *, double_stake: int | None = None,
                 timeout: float = BLACKJACK_BUTTON_TIMEOUT):
        """`double_stake` adds a Double Down button showing the second bet
        it charges — the deal's set only (`can_double`)."""
        super().__init__(timeout=timeout)
        self.author = author
        self.channel = channel
        self.guild = guild
        self.message: discord.Message | None = None
        self._fired = False
        self.add_item(_BlackjackButton(label="Hit", action="hit", style=discord.ButtonStyle.primary))
        self.add_item(_BlackjackButton(label="Stand", action="stand", style=discord.ButtonStyle.success))
        if double_stake is not None:
            self.add_item(_BlackjackButton(
                label=f"Double Down · {double_stake:,} 🪙", action="double",
                style=discord.ButtonStyle.danger,
            ))

    async def retire(self) -> None:
        """Stop taking clicks and strip the buttons: the hand moved on
        without them (typed input, `!stop`, a later turn). The guard flips
        before the edit so a click racing the strip is still refused."""
        if self._fired:
            return
        self._fired = True
        self.stop()
        if self.message is not None:
            await self._strip()

    async def attach(self, message) -> None:
        """Bind the message these buttons were sent on."""
        self.message = message
        if self._fired:  # retired while the send was in flight
            await self._strip()

    async def _strip(self) -> None:
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self._fired or self.message is None:
            return
        await self._strip()


class _BlackjackButton(discord.ui.Button):
    def __init__(self, *, label: str, action: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view: BlackjackView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.author.id:
            await interaction.response.send_message(
                "Not your hand — run `!blackjack` for your own.", ephemeral=True,
            )
            return
        gid = view.guild.id if view.guild else None
        if view._fired or is_silenced(interaction.user.id, gid):
            await interaction.response.defer()
            return
        view._fired = True
        view.stop()
        await interaction.response.edit_message(view=None)
        # The hand is re-read from state inside: if it ended while the strip
        # was in flight (a typed `stand`, `!stop`), the click is a no-op.
        if self.action == "hit":
            await blackjack_hit(view.author, view.channel, view.guild)
        elif self.action == "double":
            await blackjack_double(view.author, view.channel, view.guild)
        else:
            await blackjack_stand(view.author, view.channel, view.guild)


async def retire_blackjack_buttons(game: dict) -> None:
    """Strip the hand's current Hit/Stand buttons, if any."""
    view = game.pop("view", None)
    if view is not None:
        await view.retire()


# ── Turn actions ──────────────────────────────────────────────────────────────
# Shared by the buttons and the typed `hit` / `stand` interceptor in events.
# Each re-reads the hand from state and mutates it before its first await, so
# a click and a typed word landing together are two ordinary actions on the
# same hand (or one no-op, if the first ended it) — never a double payout.

def _playable_game(uid: int) -> dict | None:
    """The hand `uid` can act on right now: dealt and paid for."""
    game = state.active_blackjack_games.get(uid)
    if game is None or game.get("pending"):
        return None
    return game


async def _send_turn(author, channel, guild, game: dict, display: str) -> None:
    """Post the hand's next prompt with a fresh Hit/Stand set, retiring the
    previous turn's. The new set is registered before any await so a
    concurrent action retires *it* rather than leaving an orphan behind."""
    old = game.get("view")
    view = BlackjackView(author, channel, guild,
                         double_stake=game["amount"] if can_double(game) else None)
    game["view"] = view
    if old is not None:
        await old.retire()
    msg = await channel.send(
        embed=emb("🃏 Blackjack", display + "\n\n" + _turn_prompt(game), C_BLUE), view=view, silent=True,
    )
    await view.attach(msg)


async def blackjack_hit(author, channel, guild) -> None:
    """Draw one card for `author`'s hand and announce the turn: the next
    Hit/Stand prompt, a bust, or (at 21) the dealer's play-out."""
    uid = author.id
    game = _playable_game(uid)
    if game is None:
        return
    game["player_hand"].append(draw_card(game["deck"]))
    pval = hand_value(game["player_hand"])
    display = build_blackjack_display(
        game["player_hand"], game["dealer_hand"], pval, hide_dealer=True,
        username=author.display_name,
    )
    if pval > 21:
        await _bust(author, channel, guild, game, display)
    elif pval == 21:
        await _stand(author, channel, guild, game)
    else:
        await _send_turn(author, channel, guild, game, display)


async def blackjack_stand(author, channel, guild) -> None:
    """Hold `author`'s hand: the dealer plays out and the bet settles."""
    game = _playable_game(author.id)
    if game is None:
        return
    await _stand(author, channel, guild, game)


async def blackjack_double(author, channel, guild) -> None:
    """Double down: charge a second bet equal to the first, draw exactly one
    card, then stand (or bust). First decision only — see `can_double`."""
    uid = author.id
    game = _playable_game(uid)
    if game is None:
        return
    if not can_double(game):
        await channel.send(embed=emb(
            "🃏 Blackjack", "Double down is only offered on your first two cards.", C_GOLD,
        ), silent=True)
        return
    amount = game["amount"]
    # Lock the hand while the second bet is charged: shop_charge yields, and
    # a hit, stand, or second double landing in that window would settle or
    # grow a hand whose stake is about to change. Same flag as the deal —
    # "a charge for this hand is in flight" — so both input paths ignore it.
    game["pending"] = True
    charged = await shop_charge(channel, uid, amount)
    game.pop("pending", None)
    if state.active_blackjack_games.get(uid) is not game:
        # !stop forfeited the hand mid-charge. The first bet went with it;
        # the second never joined a hand, so it goes back.
        if charged:
            await shop_payout(uid, amount)
        return
    display = build_blackjack_display(
        game["player_hand"], game["dealer_hand"], hand_value(game["player_hand"]),
        hide_dealer=True, username=author.display_name,
    )
    if not charged:
        # The refusal is on screen; put the hand's buttons back under it.
        await _send_turn(author, channel, guild, game, display)
        return
    game["amount"] = amount * 2
    game["doubled"] = True
    game["player_hand"].append(draw_card(game["deck"]))
    pval = hand_value(game["player_hand"])
    if pval > 21:
        display = build_blackjack_display(
            game["player_hand"], game["dealer_hand"], pval, hide_dealer=True,
            username=author.display_name,
        )
        await _bust(author, channel, guild, game, display)
    else:
        await _stand(author, channel, guild, game)


async def _bust(author, channel, guild, game: dict, display: str) -> None:
    uid = author.id
    state.active_blackjack_games.pop(uid, None)
    await retire_blackjack_buttons(game)
    amount = game["amount"]
    if uid not in state.godmode_users:
        await record_gambling_event(guild.id if guild else None, uid, lost=amount)
    await channel.send(embed=emb(
        "💥 Bust!",
        display + _doubled_note(game)
        + f"\n\n**{author.display_name}** loses **{amount:,} 🪙**. Balance: {await get_balance(uid):,} 🪙",
        C_RED,
    ), silent=True)


async def _stand(author, channel, guild, game: dict) -> None:
    uid = author.id
    dealer = game["dealer_hand"]
    player = game["player_hand"]
    deck = game["deck"]

    dealer_play(deck, dealer)

    pval = hand_value(player)
    dval = hand_value(dealer)
    amount = game["amount"]
    uid_name = author.display_name
    gid = guild.id if guild else None
    state.active_blackjack_games.pop(uid, None)
    await retire_blackjack_buttons(game)

    display = (build_blackjack_display(player, dealer, pval, hide_dealer=False, dval=dval, username=uid_name)
               + _doubled_note(game))

    new_bj_record = False
    new_bal_record = False
    bj_winnings = 0
    if dval > 21 or pval > dval:
        bj_winnings = amount * 2
        new_bal_record = await shop_payout(uid, bj_winnings, guild_id=gid, holder_name=uid_name)
        if uid not in state.godmode_users:
            await record_gambling_event(gid, uid, gained=amount)  # net: paid `amount` via shop_charge, received 2x
        new_bj_record = await try_set_record(gid, "blackjack", bj_winnings, uid, uid_name,
                       player_hand=format_hand(player), player_score=pval,
                       dealer_score=dval)
        color, result = C_GREEN, f"✅ **{uid_name}** wins **{amount:,} 🪙**! Balance: {await get_balance(uid):,} 🪙"
    elif pval == dval:
        await shop_payout(uid, amount)  # push: bet refunded, no P/L recorded
        color, result = C_GOLD, f"🤝 Push! Bet returned. Balance: {await get_balance(uid):,} 🪙"
    else:
        if uid not in state.godmode_users:
            await record_gambling_event(gid, uid, lost=amount)
        color, result = C_RED, f"❌ Dealer wins. **{uid_name}** loses **{amount:,} 🪙**. Balance: {await get_balance(uid):,} 🪙"

    await channel.send(embed=emb("🃏 Blackjack", display + f"\n\n{result}", color), silent=True)
    if new_bj_record:
        await announce_record(channel, "blackjack", uid_name, bj_winnings, holder_id=uid)
    if new_bal_record:
        await announce_record(channel, "highest_balance", uid_name, await get_total_balance(uid), holder_id=uid)



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
            await ctx.send(embed=emb(
                "🃏 Already Playing", "Use the buttons under your hand, or type `hit` / `stand`.", C_GOLD,
            ))
            return
        # Claim the slot synchronously before the charge: shop_charge yields,
        # so two rapid !bj would otherwise both pass the gate, both get
        # charged, and the second game would overwrite (orphan) the first bet.
        deck = new_deck()
        player = [draw_card(deck), draw_card(deck)]
        dealer = [draw_card(deck), draw_card(deck)]
        pval = hand_value(player)
        dval = hand_value(dealer)

        game = {
            "amount": amount,
            "player_hand": player,
            "dealer_hand": dealer,
            "deck": deck,
            "channel_id": ctx.channel.id,
            "pending": True,  # not yet paid — hit/stand ignore it
        }
        state.active_blackjack_games[uid] = game
        if not await shop_charge(ctx, uid, amount):
            state.active_blackjack_games.pop(uid, None)
            return
        game.pop("pending", None)

        username = ctx.author.display_name
        display = build_blackjack_display(player, dealer, pval, hide_dealer=True, username=username)

        # Natural blackjack
        if pval == 21:
            del state.active_blackjack_games[uid]
            full_display = build_blackjack_display(player, dealer, pval, hide_dealer=False, dval=dval, username=username)
            if dval == 21:
                await shop_payout(uid, amount)  # push: bet refunded, no P/L recorded
                await ctx.send(embed=emb("🃏 Blackjack — Push", full_display + "\n\nBoth have Blackjack! Bet returned.", C_GOLD))
            else:
                winnings = int(amount * BLACKJACK_NATURAL_MULT)
                gid = ctx.guild.id if ctx.guild else None
                new_bal_record = await shop_payout(uid, winnings, guild_id=gid, holder_name=username)
                if uid not in state.godmode_users:
                    await record_gambling_event(gid, uid, gained=max(0, winnings - amount))
                new_bj_record = await try_set_record(gid, "blackjack", winnings, uid, username,
                               player_hand=format_hand(player), player_score=pval,
                               dealer_score=dval)
                new_bal = await get_balance(uid)
                await ctx.send(embed=emb("🃏 Blackjack!", full_display + f"\n\n**{ctx.author.display_name}** wins **{winnings:,} 🪙**! Balance: {new_bal:,} 🪙", C_GREEN))
                if new_bj_record:
                    await announce_record(ctx.channel, "blackjack", username, winnings, holder_id=uid)
                if new_bal_record:
                    await announce_record(ctx.channel, "highest_balance", username, await get_total_balance(uid), holder_id=uid)
            return

        # The buttons go on only after the charge lands, so a declined bet
        # never leaves an orphan view (and its timeout task) behind.
        await _send_turn(ctx.author, ctx.channel, ctx.guild, game, display)



async def setup(bot):
    await bot.add_cog(BlackjackCog(bot))
