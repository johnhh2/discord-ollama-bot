import asyncio
import random

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_ORANGE, _render_race,
)
from src.economy import (
    add_balance, deduct_balance, record_gambling_event,
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
                    await record_gambling_event(gid, winners[0], gained=max(0, share - amount))
                    for loser in game["players"]:
                        if loser != winners[0]:
                            await record_gambling_event(gid, loser, lost=amount)
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
                            await record_gambling_event(gid, w, gained=net)
                        elif net < 0:
                            await record_gambling_event(gid, w, lost=-net)
                    for loser in game["players"]:
                        if loser not in winner_set:
                            await record_gambling_event(gid, loser, lost=amount)
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

        # Parse optional amount (any numeric arg that isn't a mention)
        amount = 0
        for a in args:
            if not a.startswith("<@"):
                try:
                    amount = int(a)
                    if amount <= 0:
                        await ctx.send(embed=emb("❌ Invalid Amount", "Amount must be positive.", C_RED))
                        return
                except ValueError:
                    pass

        all_players = [uid] + [u.id for u in invited_users]

        # Deduct bets from all players upfront
        paid = []
        if amount > 0:
            for player_uid in all_players:
                if not await deduct_balance(player_uid, amount):
                    for refund_uid in paid:
                        await add_balance(refund_uid, amount)
                    member = ctx.guild.get_member(player_uid) if ctx.guild else None
                    name = member.display_name if member else str(player_uid)
                    await ctx.send(embed=emb("💸 Insufficient Funds", f"**{name}** can't cover the **{amount:,} 🪙** bet.", C_RED))
                    return
                paid.append(player_uid)

        # Skip confirmation if no bet
        if amount == 0:
            confirmed_ids = set(u.id for u in invited_users)
        else:
            confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title="🏇 Race Invite")

        # Refund anyone who didn't confirm
        declined = set(u.id for u in invited_users) - confirmed_ids
        if amount > 0:
            for d_uid in declined:
                await add_balance(d_uid, amount)

        if not confirmed_ids:
            if amount > 0:
                await add_balance(uid, amount)
                msg = f"Race cancelled — no one accepted the invite. Coins refunded ({amount:,} 🪙)."
            else:
                msg = "Race cancelled — no one accepted the invite."
            await ctx.send(embed=emb("❌ No One Joined", msg, C_RED))
            return

        # Build final player list (host + confirmed)
        final_players = [uid] + list(confirmed_ids)

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

        state.active_race_games[cid] = {
            "players": final_players,
            "names": names,
            "positions": {p: 0 for p in final_players},
            "amount": amount,
        }

        board = _render_race(state.active_race_games[cid])
        race_msg = await ctx.send(embed=emb("🏇 Race Starting!", board, C_ORANGE))

        asyncio.create_task(_run_race(ctx.channel, cid, race_msg))



async def setup(bot):
    await bot.add_cog(RaceCog(bot))
