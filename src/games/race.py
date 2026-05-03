import asyncio
import json
import os
import random
import time
import datetime
import logging
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import ui

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_ORANGE, C_BLUE, C_PURPLE, C_GREY,
    mocking_font, curse_font, parse_amount, send_ephemeral, resolve_role,
    fetch_member, toggle_member_role, shop_charge, _render_race,
    _delete_after, _edit_board, get_memory_mb, format_uptime, get_version,
    get_system_prompt, _log_audit, log_bot_permission_error,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, drain_bot_balance_into_lottery, announce_new_lottery,
    is_insured, get_guild_ask_model, get_guild_roleplay_model,
    get_guild_coding_model, _ct_now, _ct_today, do_daily_reset, _ensure_user,
)
from src.permissions import (
    is_admin, is_server_admin, can_manage_settings, check_rate_limit,
    check_channel, check_game_channel, check_ai_channel, check_puzzle_channel,
    check_chess_channel, _wrong_channel_reply,
)
from src.persistence import (
    _load_json, save_economy, save_insurance, save_guild_settings,
    save_bot_settings, save_godmode_users, save_bot_roles,
    save_chess_games, save_ragebait, save_mock, save_rigged_slots,
    save_gambler_streak,
    save_quote_log, save_saved_quotes, save_tax, save_curse, save_lottery,
    load_lottery, load_saved_quotes, get_guild_cfg,
)
from src.cogs.ai_cog import _wait_for_confirmations
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, SYSTEM_PROMPT, HISTORY_LIMIT,
    RATE_LIMIT_SECONDS, RACE_TRACK_LEN,
    ACTIVE_CHANNEL_IDS, DISCORD_TOKEN,
    SLOT_REEL, SLOT_JACKPOT_SEED, SLOT_JACKPOT_CONTRIB, SLOT_HOUSE_CHANCE,
    SLOT_MIN_BET, SLOT_MULT_JACKPOT, SLOT_MULT_3BAR, SLOT_MULT_3BELL,
    SLOT_MULT_3LEMON, SLOT_MULT_3CHERRY, SLOT_MULT_2CHERRY, SLOT_MULT_1CHERRY,
    SLOT_JACKPOT_BONUS_MIN_BET, SLOT_JACKPOT_BONUS_MAX_BET, SLOT_JACKPOT_BONUS_MAX_MULT,
    HANGMAN_MAX_WRONG, HANGMAN_BASE_REWARD, HANGMAN_LENGTH_OFFSET,
    HANGMAN_LENGTH_MULT, HANGMAN_UNIQUE_MULT, HANGMAN_RARE_MULT, HANGMAN_ULTRA_RARE_MULT,
    BLACKJACK_NATURAL_MULT, SCRATCH_SYMBOLS, SCRATCHOFF_MAX_DAILY, SCRATCHOFF_PAYOUTS,
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_CHANNEL_COST, SHOP_INSURANCE_COST, SHOP_TAX_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_IDS,
)
from src import state


def advance_player(pos: int, delta: int, finish: int = RACE_TRACK_LEN) -> int:
    """Move a racer forward by `delta`, clamping to the finish line."""
    return min(pos + delta, finish)


async def _run_race(channel, cid: int, race_msg: discord.Message):
    """Animate and run a race until there's a winner."""
    import random

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

            if len(winners) == 1:
                winner_name = game["names"][winners[0]]
                if share > 0:
                    await add_balance(winners[0], share)
                result = f"{board}\n\n🏆 **{winner_name}** wins" + (f" **{share:,} 🪙**!" if share else "!")
            else:
                for w in winners:
                    if share > 0:
                        await add_balance(w, share)
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
