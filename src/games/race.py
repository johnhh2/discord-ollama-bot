import asyncio
import logging
import random

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_ORANGE, C_GREY, _render_race, parse_int_amount,
    shop_charge, shop_payout, announce_record,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_total_balance,
    record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import try_set_record
from src.invites import _wait_for_confirmations
from src.dailies import keep_in_dailies_channel
from src.gambling.play_again import PlayAgainView
from src.config import (
    RACE_TRACK_LEN,
)
from src import state

# Seconds between board redraws. Module-level so tests can zero it.
RACE_TICK_SECONDS = 1.5


def settle_tick(
    positions: dict[int, int], rolls: dict[int, int], finish: int = RACE_TRACK_LEN,
) -> list[int]:
    """Apply one tick of `rolls` to `positions` in place; return the winners.

    Horses that would run past the line this tick are ranked by how far past
    it they'd have gone: the furthest wins and is drawn on the line, the rest
    are drawn one square short so the board shows the photo finish. Clamping
    every crosser to the line made any shared crossing tick a tie; now only
    horses that would have run the exact same distance past the line tie.
    """
    raw = {uid: positions[uid] + rolls[uid] for uid in positions}
    crossers = [uid for uid, at in raw.items() if at >= finish]
    if not crossers:
        positions.update(raw)
        return []
    furthest = max(raw[uid] for uid in crossers)
    for uid, at in raw.items():
        if at >= finish:
            positions[uid] = finish if at == furthest else finish - 1
        else:
            positions[uid] = at
    return [uid for uid in crossers if raw[uid] == furthest]


async def _run_race(channel, cid: int, race_msg: discord.Message):
    """Animate and run a race until there's a winner."""

    game = state.active_race_games[cid]
    try:
        await _run_race_loop(cid, race_msg, game)
    except Exception:
        # A deleted board message (or any Discord error) mid-race would
        # otherwise kill this task silently, leaving the channel locked
        # forever and every bet unpaid. Release the slot and refund.
        logging.exception("[race] race in channel %s aborted; refunding bets", cid)
        if state.active_race_games.get(cid) is game:
            del state.active_race_games[cid]
        amount = game.get("amount", 0)
        if amount > 0:
            for uid in game["players"]:
                await add_balance(uid, amount)


def _tick_rolls(players: list[int]) -> dict[int, int]:
    """One tick's movement per lane. Split out so tests can script a race."""
    return {uid: random.randint(1, 3) for uid in players}


async def _animate_race(game: dict, race_msg: discord.Message, live) -> list[int] | None:
    """Tick the race until someone crosses the line, redrawing the board
    each tick. Returns the winners, or None when `live()` turns false
    mid-race (a `!stop` pulled the game from the registry)."""
    while live():
        await asyncio.sleep(RACE_TICK_SECONDS)
        if not live():
            break
        # Advance each player; a photo finish is settled inside settle_tick.
        winners = settle_tick(game["positions"], _tick_rolls(game["players"]))
        if winners:
            return winners
        await race_msg.edit(embed=emb("🏇 Race in Progress", _render_race(game), C_ORANGE))
    return None


async def _run_race_loop(cid: int, race_msg: discord.Message, game: dict):
    winners = await _animate_race(game, race_msg, lambda: cid in state.active_race_games)
    if winners is None:
        return
    # No await between the finishing tick and this release, so a `!stop`
    # can't slip in and refund a race that's already been decided.
    del state.active_race_games[cid]
    board = _render_race(game)
    amount = game["amount"]
    total_pot = amount * len(game["players"])
    share = total_pot // len(winners)
    gid = race_msg.guild.id if race_msg.guild else None

    if len(winners) == 1:
        winner_name = game["names"][winners[0]]
        if share > 0:
            await add_balance(winners[0], share)
        if amount > 0:
            await record_gambling_event(gid, winners[0], gained=max(0, share - amount), channel_id=cid)
            for loser in game["players"]:
                if loser != winners[0]:
                    await record_gambling_event(gid, loser, lost=amount, channel_id=cid)
        result = f"{board}\n\n🏆 **{winner_name}** wins" + (f" **{share:,} 🪙**!" if share else "!")
    else:
        for w in winners:
            if share > 0:
                await add_balance(w, share)
        if amount > 0:
            winner_set = set(winners)
            for w in winners:
                net = share - amount
                if net > 0:
                    await record_gambling_event(gid, w, gained=net, channel_id=cid)
                elif net < 0:
                    await record_gambling_event(gid, w, lost=-net, channel_id=cid)
            for loser in game["players"]:
                if loser not in winner_set:
                    await record_gambling_event(gid, loser, lost=amount, channel_id=cid)
        names = ", ".join(f"**{game['names'][w]}**" for w in winners)
        result = f"{board}\n\n🤝 Tie! {names} each get **{share:,} 🪙**"

    await race_msg.edit(embed=emb("🏁 Race Finished!", result, C_GREEN))


async def play_bot_race(author, channel, guild, amount: int, bot_lane, *,
                        record_exclude: int = 0, play_again: bool = False):
    """Race `author` against the bot for `amount` — a coin flip with a track.

    The bot is the house: it stakes nothing and holds no balance, so it
    never goes through the affordability check or the payout. A win pays
    the player 2×amount, a loss forfeits the stake, and a photo-finish tie
    is a push (stake refunded). `amount` may be 0 for a free race.

    Nothing is registered in state.active_race_games: this is a personal
    gamble like !flip, so any number can run in one channel at once and
    `!stop` doesn't apply. Shared by `!race @Bot [amount]` and the dailies
    🏇 claim, which is why it takes (author, channel, guild) rather than a
    Context. `bot_lane` is the bot's identity in this guild (a Member, or
    the client user as a fallback) — only .id and .display_name are read.

    `record_exclude` and `play_again` work as in play_flip: the dailies
    claim passes its property-revenue portion as the former so auto-staked
    income can't set the "biggest race payout" record, and `!race @Bot`
    passes the latter for the "Race Again" / "2x" buttons.
    """
    uid = author.id
    # shop_charge only uses ctx.send, so the channel satisfies it. Godmode
    # users race for free — and, via shop_payout, win nothing.
    if not await shop_charge(channel, uid, amount):
        return
    game = {
        "players": [uid, bot_lane.id],
        "names": {uid: author.display_name, bot_lane.id: bot_lane.display_name},
        "positions": {uid: 0, bot_lane.id: 0},
        "amount": amount,
    }
    try:
        race_msg = await channel.send(
            embed=emb("🏇 Race Starting!", _render_race(game), C_ORANGE), silent=True,
        )
        winners = await _animate_race(game, race_msg, lambda: True)
    except Exception:
        # A deleted board (or any Discord error) mid-race must not eat the
        # stake — same rule as the multiplayer race.
        logging.exception("[race] bot race for user %s aborted; refunding stake", uid)
        await shop_payout(uid, amount)
        return

    gid = guild.id if guild else None
    won = set(winners) == {uid}
    tied = len(winners) > 1
    payout = amount * 2 if won else amount if tied else 0
    net = payout - amount
    new_bal_record = False
    if payout:
        new_bal_record = await shop_payout(uid, payout, guild_id=gid, holder_name=author.display_name)
    if uid not in state.godmode_users:
        if net > 0:
            await record_gambling_event(gid, uid, gained=net, channel_id=channel.id)
        elif net < 0:
            await record_gambling_event(gid, uid, lost=-net, channel_id=channel.id)
    new_race_record = False
    record_payout = max(0, amount - record_exclude) * 2
    if won and record_payout > 0:
        new_race_record = await try_set_record(gid, "race", record_payout, uid, author.display_name)

    you = author.display_name
    bal_line = f" Balance: {await get_balance(uid):,} 🪙" if amount else ""
    if won:
        line = f"🏆 **{you}** wins" + (f" **{payout:,} 🪙**!" if amount else "!") + bal_line
        color = C_GREEN
    elif tied:
        line = "🤝 Photo finish — a tie!" + (f" **{you}** gets the **{amount:,} 🪙** stake back." if amount else "") + bal_line
        color = C_GREY
    else:
        line = f"🤖 **{bot_lane.display_name}** wins" + (f" — **{you}** lost **{amount:,} 🪙**." if amount else "!") + bal_line
        color = C_RED

    view = None
    if play_again:
        async def _replay(stake: int):
            await play_bot_race(author, channel, guild, stake, bot_lane, play_again=True)

        if amount:
            options = [(f"Race Again · {amount:,} 🪙", amount), (f"2x · {amount * 2:,} 🪙", amount * 2)]
        else:
            options = [("Race Again · free", 0)]
        view = PlayAgainView(
            author, guild, replay=_replay, options=options,
            not_yours="Not your race — run `!race @Bot` for your own.",
        )
    edit_kwargs = {"view": view} if view is not None else {}
    try:
        await race_msg.edit(embed=emb("🏁 Race Finished!", f"{_render_race(game)}\n\n{line}", color), **edit_kwargs)
    except discord.HTTPException:
        # The payout already landed; a vanished board only costs the buttons.
        logging.warning("[race] bot race result edit failed for user %s", uid)
        if view is not None:
            view.stop()
            view = None
    if view is not None:
        view.message = race_msg  # on_timeout strips the buttons from it

    # Big results in the dailies channel stay pinned until the 5am reset.
    await keep_in_dailies_channel(guild, channel, race_msg, net)

    if new_race_record:
        await announce_record(channel, "race", author.display_name, record_payout, holder_id=uid)
    if new_bal_record:
        await announce_record(channel, "highest_balance", author.display_name, await get_total_balance(uid), holder_id=uid)



class RaceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="race")
    async def cmd_race(self, ctx: commands.Context, *args):
        if await check_game_channel(ctx):
            return
        cid = ctx.channel.id
        uid = ctx.author.id

        invited_users = [m for m in ctx.message.mentions if m.id != uid]
        if not invited_users:
            await ctx.send("Usage: `!race @user1 [@user2 ...] [amount]` — or `!race @Bot [amount]` to race the bot.")
            return

        # Parse optional amount (any non-mention arg). parse_int_amount
        # handles the k/m shorthand — raw int() made `!race @user 10k`
        # silently a free race.
        amount = 0
        for a in args:
            if not a.startswith("<@"):
                parsed = parse_int_amount(a)
                if parsed is None or parsed <= 0:
                    await ctx.send(embed=emb("❌ Invalid Amount", "Amount must be a positive whole number (e.g. `100`, `2.5k`).", C_RED))
                    return
                amount = parsed

        bot_user = self.bot.user if self.bot is not None else None
        bot_lane = next((m for m in invited_users if bot_user is not None and m.id == bot_user.id), None)
        if bot_lane is not None:
            if len(invited_users) > 1:
                await ctx.send(embed=emb(
                    "❌ Bot Races Are One-on-One",
                    f"Race the bot by itself: `!race @{bot_lane.display_name} [amount]`.",
                    C_RED,
                ))
                return
            # The house race is a personal gamble like !flip: no channel
            # slot, no invite window, no !stop — see play_bot_race.
            await play_bot_race(ctx.author, ctx.channel, ctx.guild, amount, bot_lane, play_again=True)
            return

        if cid in state.active_ttt_games or cid in state.active_c4_games or cid in state.active_race_games:
            await ctx.send(embed=emb("❌ Game Active", "Finish the current game first.", C_RED))
            return

        all_players = [uid] + [u.id for u in invited_users]

        # Claim the channel slot synchronously before the bet/confirmation
        # awaits: two !race invocations could otherwise both pass the gate,
        # and the second game would clobber the first — whose task then
        # deletes the second game, eating its bets.
        placeholder = {"players": [], "names": {}, "positions": {}, "amount": 0}
        state.active_race_games[cid] = placeholder

        try:
            # Check affordability without charging. Debiting every invitee
            # before they agreed let one !race freeze several players' balances
            # for the invite window — and nothing stopped the same victims
            # being named from several channels at once.
            if amount > 0:
                for player_uid in all_players:
                    if await get_balance(player_uid) < amount:
                        member = ctx.guild.get_member(player_uid) if ctx.guild else None
                        name = member.display_name if member else str(player_uid)
                        await ctx.send(embed=emb("💸 Insufficient Funds", f"**{name}** can't cover the **{amount:,} 🪙** bet.", C_RED))
                        return

            # Skip confirmation if no bet
            if amount == 0:
                confirmed_ids = set(u.id for u in invited_users)
            else:
                confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title="🏇 Race Invite")

            if not confirmed_ids:
                await ctx.send(embed=emb(
                    "❌ No One Joined", "Race cancelled — no one accepted the invite.", C_RED,
                ))
                return

            # Build final player list (host + confirmed)
            final_players = [uid] + list(confirmed_ids)

            # Charge only the players who are actually racing, now that the
            # roster is final. Balances may have moved during the invite
            # window, so unwind everyone already debited if one can't pay.
            if amount > 0:
                paid = []
                for player_uid in final_players:
                    if not await deduct_balance(player_uid, amount):
                        for refund_uid in paid:
                            await add_balance(refund_uid, amount)
                        member = ctx.guild.get_member(player_uid) if ctx.guild else None
                        name = member.display_name if member else str(player_uid)
                        await ctx.send(embed=emb("💸 Insufficient Funds", f"**{name}** can't cover the **{amount:,} 🪙** bet.", C_RED))
                        return
                    paid.append(player_uid)

            # Build names map using known member objects where available
            names = {}
            # Host is always ctx.author
            names[uid] = ctx.author.display_name
            # Confirmed players come from invited_users
            for player_uid in confirmed_ids:
                # Find the member from invited_users
                member = next((u for u in invited_users if u.id == player_uid), None)
                if member:
                    names[player_uid] = member.display_name
                else:
                    # Fallback to lookup (shouldn't happen)
                    member = ctx.guild.get_member(player_uid) if ctx.guild else None
                    names[player_uid] = member.display_name if member else str(player_uid)

            # Replacing the placeholder with the real game keeps the slot
            # claimed; the finally below then leaves it in place.
            state.active_race_games[cid] = {
                "players": final_players,
                "names": names,
                "positions": {p: 0 for p in final_players},
                "amount": amount,
            }
        finally:
            # Early return or exception anywhere above: release the slot
            # (identity check — the success path already swapped in the game).
            if state.active_race_games.get(cid) is placeholder:
                del state.active_race_games[cid]

        board = _render_race(state.active_race_games[cid])
        race_msg = await ctx.send(embed=emb("🏇 Race Starting!", board, C_ORANGE))

        asyncio.create_task(_run_race(ctx.channel, cid, race_msg))



async def setup(bot):
    await bot.add_cog(RaceCog(bot))
