import asyncio

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
    _delete_after, _edit_board, parse_int_amount,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.invites import _wait_for_confirmations
from src import state


NUM_EMOJIS_TTT = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣"]
NUM_EMOJIS_C4 = NUM_EMOJIS_TTT[:7]


def build_ttt_display(game: dict) -> str:
    """Build a tic-tac-toe board display from game state."""
    board = game["board"]
    row1 = (board[0] or NUM_EMOJIS_TTT[0]) + (board[1] or NUM_EMOJIS_TTT[1]) + (board[2] or NUM_EMOJIS_TTT[2])
    row2 = (board[3] or NUM_EMOJIS_TTT[3]) + (board[4] or NUM_EMOJIS_TTT[4]) + (board[5] or NUM_EMOJIS_TTT[5])
    row3 = (board[6] or NUM_EMOJIS_TTT[6]) + (board[7] or NUM_EMOJIS_TTT[7]) + (board[8] or NUM_EMOJIS_TTT[8])
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

async def _add_initial_reactions(channel, msg_id: int, game_type: str) -> None:
    """Add the bot's number-emoji reactions to a freshly-posted board message."""
    try:
        msg = await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.HTTPException):
        return
    emojis = NUM_EMOJIS_TTT if game_type == "ttt" else NUM_EMOJIS_C4
    for e in emojis:
        try:
            await msg.add_reaction(e)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return


async def _fetch_board_msg(channel, game: dict):
    """Fetch the board message, or None if it's gone or inaccessible."""
    if game.get("board_msg_id") is None:
        return None
    try:
        return await channel.fetch_message(game["board_msg_id"])
    except (discord.NotFound, discord.HTTPException):
        return None


async def _remove_user_reaction(channel, game: dict, emoji: str, user) -> None:
    """Remove just one user's reaction for one emoji — keeps the bot's reaction in place
    so the button stays clickable for the next move."""
    msg = await _fetch_board_msg(channel, game)
    if msg is None:
        return
    try:
        await msg.remove_reaction(emoji, user)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _remove_bot_reaction(channel, game: dict, emoji: str, bot_user) -> None:
    """Remove the bot's own reaction for one emoji — used when a move is no longer
    legal (square taken in TTT, column full in C4) so the button disappears."""
    msg = await _fetch_board_msg(channel, game)
    if msg is None:
        return
    try:
        await msg.remove_reaction(emoji, bot_user)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def _clear_all_reactions(channel, game: dict) -> None:
    """Clear every reaction on the board — used only when the game ends."""
    msg = await _fetch_board_msg(channel, game)
    if msg is None:
        return
    try:
        await msg.clear_reactions()
    except (discord.Forbidden, discord.HTTPException):
        return


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

    `amount` is the already-parsed wager (int >= 0); callers parse the raw
    string (with `k`/`m` shorthand) before invoking. Returns True if the game
    should proceed; False if an error was already sent."""
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


async def _apply_ttt_move(channel, guild, mover, pos: int | None) -> None:
    """Apply a TTT move and update the board. Sends temp error embeds for invalid moves.

    `mover` is the discord.User/Member who made the move — needed to remove their
    reaction after a successful click so they can react again on their next turn."""
    uid = mover.id
    name = mover.display_name if hasattr(mover, "display_name") else str(mover)
    cid = channel.id
    if cid not in state.active_ttt_games:
        return
    game = state.active_ttt_games[cid]
    if uid != game["current"]:
        err = await channel.send(embed=emb("⏳ Not Your Turn", f"Waiting for {guild.get_member(game['current']).mention if guild else 'opponent'}.", C_GOLD))
        asyncio.create_task(_delete_after(err))
        return
    if pos is None or not 1 <= pos <= 9:
        err = await channel.send("Use `!m <1-9>` to place your mark.")
        asyncio.create_task(_delete_after(err))
        return
    idx = pos - 1
    if game["board"][idx] is not None:
        err = await channel.send(embed=emb("❌ Taken", "That square is already taken.", C_RED))
        asyncio.create_task(_delete_after(err))
        return
    game["board"][idx] = game["marks"][uid]
    move_emoji = NUM_EMOJIS_TTT[idx]
    bot_user = guild.me if guild else None
    winner = check_ttt_winner(game["board"])
    if winner:
        winner_uid = [p for p in game["players"] if game["marks"][p] == winner][0]
        amount = game.get("amount", 0)
        winnings = amount * 2
        if winnings > 0:
            await add_balance(winner_uid, winnings)
        if amount > 0:
            loser_uid = next(p for p in game["players"] if p != winner_uid)
            gid = guild.id if guild else None
            await record_gambling_event(gid, winner_uid, gained=amount)
            await record_gambling_event(gid, loser_uid, lost=amount)
        winner_name = guild.get_member(winner_uid).display_name if guild else str(winner_uid)
        game["last_move"] = f"{name} played position {pos} — {winner_name} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "")
        winner_mention = guild.get_member(winner_uid).mention if guild else str(winner_uid)
        await _edit_board(channel, game, emb("🎉 Tic-Tac-Toe Won!", build_ttt_display(game) + f"\n\n{winner_mention} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "") + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
        await _clear_all_reactions(channel, game)
        del state.active_ttt_games[cid]
    elif all(c is not None for c in game["board"]) or is_ttt_stalemate(game["board"]):
        amount = game.get("amount", 0)
        if amount > 0:
            for player_uid in game["players"]:
                await add_balance(player_uid, amount)
        game["last_move"] = f"{name} played position {pos} — It's a draw!"
        draw_text = "\n\nIt's a draw!" + (f" Each player gets {amount:,} 🪙 back." if amount > 0 else "")
        await _edit_board(channel, game, emb("🤝 Tic-Tac-Toe Draw", build_ttt_display(game) + draw_text + f"\n\n**Last move:** {game['last_move']}", C_GOLD))
        await _clear_all_reactions(channel, game)
        del state.active_ttt_games[cid]
    else:
        players = game["players"]
        game["current"] = players[1] if uid == players[0] else players[0]
        next_player = guild.get_member(game["current"]) if guild else None
        game["last_move"] = f"{name} played position {pos}"
        await _edit_board(channel, game, emb("🎮 Tic-Tac-Toe", build_ttt_display(game) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!m <1-9>`\n\n**Last move:** {game['last_move']}", C_BLUE))
        # Square is now taken — remove the bot's reaction for that number so it stops
        # being clickable. Also remove the mover's own reaction so they can react again
        # on their next turn (Discord ignores duplicate reactions, which is the source
        # of the "click does nothing" bug).
        if bot_user is not None:
            await _remove_bot_reaction(channel, game, move_emoji, bot_user)
        await _remove_user_reaction(channel, game, move_emoji, mover)


async def _apply_c4_move(channel, guild, mover, pos: int | None) -> None:
    """Apply a C4 move and update the board. Sends temp error embeds for invalid moves.

    `mover` is the discord.User/Member who made the move — needed to remove their
    reaction after a successful click so they can react again on their next turn."""
    uid = mover.id
    name = mover.display_name if hasattr(mover, "display_name") else str(mover)
    cid = channel.id
    if cid not in state.active_c4_games:
        return
    game = state.active_c4_games[cid]
    if uid != game["current"]:
        err = await channel.send(embed=emb("⏳ Not Your Turn", f"Waiting for {guild.get_member(game['current']).mention if guild else 'opponent'}.", C_GOLD))
        asyncio.create_task(_delete_after(err))
        return
    if pos is None or not 1 <= pos <= 7:
        err = await channel.send("Use `!m <1-7>` to drop a piece.")
        asyncio.create_task(_delete_after(err))
        return
    col = pos - 1
    row = drop_in_column(game["board"], col)
    if row is None:
        err = await channel.send(embed=emb("❌ Column Full", "That column is full.", C_RED))
        asyncio.create_task(_delete_after(err))
        return
    game["board"][row][col] = game["marks"][uid]
    move_emoji = NUM_EMOJIS_C4[col]
    column_now_full = drop_in_column(game["board"], col) is None
    bot_user = guild.me if guild else None
    winner = check_c4_winner(game["board"])
    if winner:
        winner_uid = [p for p in game["players"] if game["marks"][p] == winner][0]
        amount = game.get("amount", 0)
        winnings = amount * 2
        if winnings > 0:
            await add_balance(winner_uid, winnings)
        if amount > 0:
            loser_uid = next(p for p in game["players"] if p != winner_uid)
            gid = guild.id if guild else None
            await record_gambling_event(gid, winner_uid, gained=amount)
            await record_gambling_event(gid, loser_uid, lost=amount)
        winner_name = guild.get_member(winner_uid).display_name if guild else str(winner_uid)
        game["last_move"] = f"{name} dropped in column {pos} — {winner_name} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "")
        winner_mention = guild.get_member(winner_uid).mention if guild else str(winner_uid)
        await _edit_board(channel, game, emb("🎉 Connect 4 Won!", build_c4_display(game) + f"\n\n{winner_mention} wins!" + (f" **+{winnings:,} 🪙**" if winnings > 0 else "") + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
        await _clear_all_reactions(channel, game)
        del state.active_c4_games[cid]
    elif all(game["board"][r][c] is not None for r in range(6) for c in range(7)):
        amount = game.get("amount", 0)
        if amount > 0:
            for player_uid in game["players"]:
                await add_balance(player_uid, amount)
        game["last_move"] = f"{name} dropped in column {pos} — It's a draw!"
        draw_text = "\n\nIt's a draw!" + (f" Each player gets {amount:,} 🪙 back." if amount > 0 else "")
        await _edit_board(channel, game, emb("🤝 Connect 4 Draw", build_c4_display(game) + draw_text + f"\n\n**Last move:** {game['last_move']}", C_GOLD))
        await _clear_all_reactions(channel, game)
        del state.active_c4_games[cid]
    else:
        players = game["players"]
        game["current"] = players[1] if uid == players[0] else players[0]
        next_player = guild.get_member(game["current"]) if guild else None
        game["last_move"] = f"{name} dropped in column {pos}"
        await _edit_board(channel, game, emb("🟡 Connect 4", build_c4_display(game) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!m <1-7>`\n\n**Last move:** {game['last_move']}", C_BLUE))
        # If this drop filled the column, remove the bot's reaction for that number so
        # nobody can click it again. Always remove the mover's own reaction so they can
        # react again on their next turn (Discord ignores duplicate reactions, which is
        # the source of the "click does nothing then everything fires at once" bug).
        if column_now_full and bot_user is not None:
            await _remove_bot_reaction(channel, game, move_emoji, bot_user)
        await _remove_user_reaction(channel, game, move_emoji, mover)


class TttC4Cog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ttt")
    async def cmd_ttt(self, ctx: commands.Context, opponent: discord.User = None, amount: str = "0"):
        if await check_game_channel(ctx):
            return
        cid = ctx.channel.id
        uid = ctx.author.id
        if cid in state.active_ttt_games or cid in state.active_c4_games:
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return
        amount = parse_int_amount(amount)
        if amount is None:
            await ctx.send("Amount must be a positive whole number (e.g. `100`, `2.5k`).")
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
                               "Use `!m <1-9>` or click a number reaction", amount)
        await _add_initial_reactions(ctx.channel, state.active_ttt_games[cid]["board_msg_id"], "ttt")

    @commands.command(name="c4")
    async def cmd_c4(self, ctx: commands.Context, opponent: discord.User = None, amount: str = "0"):
        if await check_game_channel(ctx):
            return
        cid = ctx.channel.id
        uid = ctx.author.id
        if cid in state.active_ttt_games or cid in state.active_c4_games:
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return
        amount = parse_int_amount(amount)
        if amount is None:
            await ctx.send("Amount must be a positive whole number (e.g. `100`, `2.5k`).")
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
                               "Use `!m <1-7>` or click a number reaction", amount)
        await _add_initial_reactions(ctx.channel, state.active_c4_games[cid]["board_msg_id"], "c4")

    @commands.command(name="m",)
    async def cmd_move(self, ctx: commands.Context, pos: int = None):
        cid = ctx.channel.id

        if cid in state.active_ttt_games:
            asyncio.create_task(_delete_after(ctx.message))
            await _apply_ttt_move(ctx.channel, ctx.guild, ctx.author, pos)
        elif cid in state.active_c4_games:
            asyncio.create_task(_delete_after(ctx.message))
            await _apply_c4_move(ctx.channel, ctx.guild, ctx.author, pos)
        else:
            err = await ctx.send(embed=emb("❌ No Game", "No active tic-tac-toe or connect 4 game in this channel.", C_GREY))
            asyncio.create_task(_delete_after(err))

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        """Translate a number-emoji reaction on a TTT/C4 board into a move."""
        if user.bot:
            return
        msg = reaction.message
        cid = msg.channel.id
        emoji = str(reaction.emoji)

        if cid in state.active_ttt_games:
            game = state.active_ttt_games[cid]
            if msg.id != game.get("board_msg_id"):
                return
            if emoji not in NUM_EMOJIS_TTT:
                return
            if user.id not in game["players"] or user.id != game["current"]:
                try:
                    await reaction.remove(user)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return
            pos = NUM_EMOJIS_TTT.index(emoji) + 1
            await _apply_ttt_move(msg.channel, msg.guild, user, pos)

        elif cid in state.active_c4_games:
            game = state.active_c4_games[cid]
            if msg.id != game.get("board_msg_id"):
                return
            if emoji not in NUM_EMOJIS_C4:
                return
            if user.id not in game["players"] or user.id != game["current"]:
                try:
                    await reaction.remove(user)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return
            pos = NUM_EMOJIS_C4.index(emoji) + 1
            await _apply_c4_move(msg.channel, msg.guild, user, pos)


async def setup(bot):
    await bot.add_cog(TttC4Cog(bot))
