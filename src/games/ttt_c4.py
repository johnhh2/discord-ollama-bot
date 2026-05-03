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
    save_economy, save_insurance, save_guild_settings,
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
    ASK_SYSTEM_PROMPT, STORY_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
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


def build_ttt_display(game: dict) -> str:
    """Build a tic-tac-toe board display from game state."""
    NUM_EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣"]
    board = game["board"]
    row1 = (board[0] or NUM_EMOJIS[0]) + (board[1] or NUM_EMOJIS[1]) + (board[2] or NUM_EMOJIS[2])
    row2 = (board[3] or NUM_EMOJIS[3]) + (board[4] or NUM_EMOJIS[4]) + (board[5] or NUM_EMOJIS[5])
    row3 = (board[6] or NUM_EMOJIS[6]) + (board[7] or NUM_EMOJIS[7]) + (board[8] or NUM_EMOJIS[8])
    return f"{row1}\n{row2}\n{row3}"


def build_c4_display(game: dict) -> str:
    """Build a connect 4 board display from game state."""
    COL_EMOJIS = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    board = game["board"]
    display = COL_EMOJIS + "\n"
    for row in board:
        display += "".join(cell or "⚫" for cell in row) + "\n"
    return display.strip()


def check_ttt_winner(board: list) -> str | None:
    """Check if there's a winner in tic-tac-toe. Return winning mark or None."""
    LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def is_ttt_stalemate(board: list) -> bool:
    """Return True if neither player can possibly win — forced draw."""
    LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    marks = {c for c in board if c is not None}
    if len(marks) < 2:
        return False
    for mark in marks:
        opponent = (marks - {mark}).pop()
        for line in LINES:
            if not any(board[i] == opponent for i in line):
                return False  # this mark can still win via this line
    return True


def drop_in_column(board: list, col: int) -> "int | None":
    """Return the row a piece would land in for `col` (gravity), or None if full.

    Connect 4 boards are 6 rows × 7 cols, indexed top-down (row 0 is the top).
    Pieces fall to the lowest empty row.
    """
    return next((r for r in range(5, -1, -1) if board[r][col] is None), None)


def check_c4_winner(board: list) -> str | None:
    """Check if there's a winner in connect 4. Return winning mark or None."""
    # Check horizontal
    for r in range(6):
        for c in range(4):
            if board[r][c] and board[r][c] == board[r][c+1] == board[r][c+2] == board[r][c+3]:
                return board[r][c]
    # Check vertical
    for r in range(3):
        for c in range(7):
            if board[r][c] and board[r][c] == board[r+1][c] == board[r+2][c] == board[r+3][c]:
                return board[r][c]
    # Check diagonal (↗)
    for r in range(3):
        for c in range(4):
            if board[r][c] and board[r][c] == board[r+1][c+1] == board[r+2][c+2] == board[r+3][c+3]:
                return board[r][c]
    # Check diagonal (↖)
    for r in range(3, 6):
        for c in range(4):
            if board[r][c] and board[r][c] == board[r-1][c+1] == board[r-2][c+2] == board[r-3][c+3]:
                return board[r][c]
    return None

async def _send_game_board(ctx: commands.Context, game: dict, title: str,
                           board_text: str, player1_desc: str, player2_desc: str,
                           controls: str, amount: int) -> None:
    """Send the initial PVP board message and store its ID in game['board_msg_id']."""
    wager_info = f"\nWager: {amount:,} 🪙 each" if amount > 0 else ""
    desc = (
        f"{board_text}\n\n"
        f"{player1_desc} vs {player2_desc}{wager_info}\n"
        f"{ctx.author.mention}'s turn. {controls}\n\n"
        f"**Last move:** {game['last_move']}"
    )
    msg = await ctx.send(embed=emb(title, desc, C_BLUE))
    game["board_msg_id"] = msg.id


async def _setup_pvp_game(ctx, opponent, amount, invite_title):
    """Validates opponent, deducts wagers, waits for confirmation.
    Returns True if game should proceed; False if an error was already sent."""
    uid = ctx.author.id
    if opponent is None:
        await ctx.send(f"Usage: `!{ctx.invoked_with} @user [amount]`")
        return False
    if opponent.id == uid:
        await ctx.send(embed=emb("❌ Can't Invite Yourself", "Pick a different opponent.", C_RED))
        return False
    if amount < 0:
        await ctx.send("Amount must be positive.")
        return False
    if amount > 0:
        if not await deduct_balance(uid, amount):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"**{ctx.author.display_name}** needs {amount:,} 🪙. Balance: {await get_balance(uid):,} 🪙", C_RED))
            return False
        if not await deduct_balance(opponent.id, amount):
            await add_balance(uid, amount)  # refund challenger
            await ctx.send(embed=emb("💸 Insufficient Funds", f"{opponent.display_name} needs {amount:,} 🪙. Balance: {await get_balance(opponent.id):,} 🪙", C_RED))
            return False
    wager_text = f" for {amount:,} 🪙" if amount > 0 else ""
    confirmed = await _wait_for_confirmations(ctx, [opponent], title=f"{invite_title}{wager_text}")
    if not confirmed:
        if amount > 0:
            await add_balance(uid, amount)
            await add_balance(opponent.id, amount)
            msg = f"{opponent.display_name} didn't accept. Coins refunded ({amount:,} 🪙 each)."
        else:
            msg = f"{opponent.display_name} didn't accept."
        await ctx.send(embed=emb("❌ Invite Declined", msg, C_RED))
        return False
    return True


class TttC4Cog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ttt")
    async def cmd_ttt(self, ctx: commands.Context, opponent: discord.User = None, amount: int = 0):
        if await check_game_channel(ctx):
            return
        cid = ctx.channel.id
        uid = ctx.author.id
        if cid in state.active_ttt_games or cid in state.active_c4_games:
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return
        if not await _setup_pvp_game(ctx, opponent, amount, "📨 Tic-Tac-Toe Invite"):
            return
        state.active_ttt_games[cid] = {
            "board": [None]*9,
            "players": [uid, opponent.id],
            "marks": {uid: "❌", opponent.id: "⭕"},
            "current": uid,
            "amount": amount,
            "board_msg_id": None,
            "last_move": f"{ctx.author.display_name}'s turn",
        }
        await _send_game_board(ctx, state.active_ttt_games[cid], "🎮 Tic-Tac-Toe",
                               build_ttt_display(state.active_ttt_games[cid]),
                               f"{ctx.author.mention} (❌)", f"{opponent.mention} (⭕)",
                               "Use `!m <1-9>`", amount)

    @commands.command(name="c4")
    async def cmd_c4(self, ctx: commands.Context, opponent: discord.User = None, amount: int = 0):
        if await check_game_channel(ctx):
            return
        cid = ctx.channel.id
        uid = ctx.author.id
        if cid in state.active_ttt_games or cid in state.active_c4_games:
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return
        if not await _setup_pvp_game(ctx, opponent, amount, "📨 Connect 4 Invite"):
            return
        state.active_c4_games[cid] = {
            "board": [[None]*7 for _ in range(6)],
            "players": [uid, opponent.id],
            "marks": {uid: "🔴", opponent.id: "🟡"},
            "current": uid,
            "amount": amount,
            "board_msg_id": None,
            "last_move": f"{ctx.author.display_name}'s turn",
        }
        await _send_game_board(ctx, state.active_c4_games[cid], "🟡 Connect 4",
                               build_c4_display(state.active_c4_games[cid]),
                               f"{ctx.author.mention} (🔴)", f"{opponent.mention} (🟡)",
                               "Use `!m <1-7>`", amount)

    @commands.command(name="m",)
    async def cmd_move(self, ctx: commands.Context, pos: int = None):
        cid = ctx.channel.id
        uid = ctx.author.id
        name = ctx.author.display_name

        if cid in state.active_ttt_games:
            game = state.active_ttt_games[cid]
            asyncio.create_task(_delete_after(ctx.message))
            if uid != game["current"]:
                err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {ctx.guild.get_member(game['current']).mention if ctx.guild else 'opponent'}.", C_GOLD))
                asyncio.create_task(_delete_after(err))
                return
            if pos is None or not 1 <= pos <= 9:
                err = await ctx.send("Use `!m <1-9>` to place your mark.")
                asyncio.create_task(_delete_after(err))
                return
            idx = pos - 1
            if game["board"][idx] is not None:
                err = await ctx.send(embed=emb("❌ Taken", "That square is already taken.", C_RED))
                asyncio.create_task(_delete_after(err))
                return
            game["board"][idx] = game["marks"][uid]
            winner = check_ttt_winner(game["board"])
            if winner:
                winner_uid = [p for p in game["players"] if game["marks"][p] == winner][0]
                amount = game.get("amount", 0)
                winnings = amount * 2
                if winnings > 0:
                    await add_balance(winner_uid, winnings)
                winner_name = ctx.guild.get_member(winner_uid).display_name if ctx.guild else str(winner_uid)
                game["last_move"] = f"{name} played position {pos} — {winner_name} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "")
                winner_mention = ctx.guild.get_member(winner_uid).mention if ctx.guild else str(winner_uid)
                await _edit_board(ctx.channel, game, emb("🎉 Tic-Tac-Toe Won!", build_ttt_display(game) + f"\n\n{winner_mention} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "") + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
                del state.active_ttt_games[cid]
            elif all(c is not None for c in game["board"]) or is_ttt_stalemate(game["board"]):
                amount = game.get("amount", 0)
                if amount > 0:
                    for player_uid in game["players"]:
                        await add_balance(player_uid, amount)
                game["last_move"] = f"{name} played position {pos} — It's a draw!"
                draw_text = f"\n\nIt's a draw!" + (f" Each player gets {amount:,} 🪙 back." if amount > 0 else "")
                await _edit_board(ctx.channel, game, emb("🤝 Tic-Tac-Toe Draw", build_ttt_display(game) + draw_text + f"\n\n**Last move:** {game['last_move']}", C_GOLD))
                del state.active_ttt_games[cid]
            else:
                players = game["players"]
                game["current"] = players[1] if uid == players[0] else players[0]
                next_player = ctx.guild.get_member(game["current"]) if ctx.guild else None
                game["last_move"] = f"{name} played position {pos}"
                await _edit_board(ctx.channel, game, emb("🎮 Tic-Tac-Toe", build_ttt_display(game) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!m <1-9>`\n\n**Last move:** {game['last_move']}", C_BLUE))

        elif cid in state.active_c4_games:
            game = state.active_c4_games[cid]
            asyncio.create_task(_delete_after(ctx.message))
            if uid != game["current"]:
                err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {ctx.guild.get_member(game['current']).mention if ctx.guild else 'opponent'}.", C_GOLD))
                asyncio.create_task(_delete_after(err))
                return
            if pos is None or not 1 <= pos <= 7:
                err = await ctx.send("Use `!m <1-7>` to drop a piece.")
                asyncio.create_task(_delete_after(err))
                return
            col = pos - 1
            row = drop_in_column(game["board"], col)
            if row is None:
                err = await ctx.send(embed=emb("❌ Column Full", "That column is full.", C_RED))
                asyncio.create_task(_delete_after(err))
                return
            game["board"][row][col] = game["marks"][uid]
            winner = check_c4_winner(game["board"])
            if winner:
                winner_uid = [p for p in game["players"] if game["marks"][p] == winner][0]
                amount = game.get("amount", 0)
                winnings = amount * 2
                if winnings > 0:
                    await add_balance(winner_uid, winnings)
                winner_name = ctx.guild.get_member(winner_uid).display_name if ctx.guild else str(winner_uid)
                game["last_move"] = f"{name} dropped in column {pos} — {winner_name} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "")
                winner_mention = ctx.guild.get_member(winner_uid).mention if ctx.guild else str(winner_uid)
                await _edit_board(ctx.channel, game, emb("🎉 Connect 4 Won!", build_c4_display(game) + f"\n\n{winner_mention} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "") + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
                del state.active_c4_games[cid]
            elif all(game["board"][r][c] is not None for r in range(6) for c in range(7)):
                amount = game.get("amount", 0)
                if amount > 0:
                    for player_uid in game["players"]:
                        await add_balance(player_uid, amount)
                game["last_move"] = f"{name} dropped in column {pos} — It's a draw!"
                draw_text = f"\n\nIt's a draw!" + (f" Each player gets {amount:,} 🪙 back." if amount > 0 else "")
                await _edit_board(ctx.channel, game, emb("🤝 Connect 4 Draw", build_c4_display(game) + draw_text + f"\n\n**Last move:** {game['last_move']}", C_GOLD))
                del state.active_c4_games[cid]
            else:
                players = game["players"]
                game["current"] = players[1] if uid == players[0] else players[0]
                next_player = ctx.guild.get_member(game["current"]) if ctx.guild else None
                game["last_move"] = f"{name} dropped in column {pos}"
                await _edit_board(ctx.channel, game, emb("🟡 Connect 4", build_c4_display(game) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!m <1-7>`\n\n**Last move:** {game['last_move']}", C_BLUE))

        else:
            err = await ctx.send(embed=emb("❌ No Game", "No active tic-tac-toe or connect 4 game in this channel.", C_GREY))
            asyncio.create_task(_delete_after(err))



async def setup(bot):
    await bot.add_cog(TttC4Cog(bot))
