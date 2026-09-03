import asyncio
import logging
import random

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_ORANGE, _render_race, parse_int_amount,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.invites import _wait_for_confirmations
from src.config import (
    RACE_TRACK_LEN,
)
from src import state


def advance_player(pos: int, delta: int, finish: int = RACE_TRACK_LEN) -> int:
    """Move a racer forward by `delta`, clamping to the finish line."""
    return min(pos + delta, finish)


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


async def _run_race_loop(cid: int, race_msg: discord.Message, game: dict):
    while cid in state.active_race_games:
        await asyncio.sleep(1.5)
        if cid not in state.active_race_games:
            break

        # Advance each player
        for uid in game["players"]:
            game["positions"][uid] = advance_player(
                game["positions"][uid], random.randint(1, 3),
            )

        # Check for winners
        winners = [uid for uid in game["players"] if game["positions"][uid] >= RACE_TRACK_LEN]
        board = _render_race(game)

        if winners:
            del state.active_race_games[cid]
            amount = game["amount"]
            total_pot = amount * len(game["players"])
            share = total_pot // len(winners) if winners else 0
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
            return

        # Update board
        await race_msg.edit(embed=emb("🏇 Race in Progress", board, C_ORANGE))



class RaceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="race")
    async def cmd_race(self, ctx: commands.Context, *args):
        if await check_game_channel(ctx):
            return
        cid = ctx.channel.id
        uid = ctx.author.id

        if cid in state.active_ttt_games or cid in state.active_c4_games or cid in state.active_race_games:
            await ctx.send(embed=emb("❌ Game Active", "Finish the current game first.", C_RED))
            return

        invited_users = [m for m in ctx.message.mentions if m.id != uid]
        if not invited_users:
            await ctx.send("Usage: `!race @user1 [@user2 ...] [amount]`")
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
