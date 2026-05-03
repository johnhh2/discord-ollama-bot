import asyncio

from discord.ext import commands

from src.helpers import (
    emb, C_RED, C_GOLD, C_BLUE, _delete_after, _edit_board,
)
from src.permissions import (
    is_admin, check_chess_channel,
)
from src.persistence import (
    save_chess_games,
)
from src import state


def create_chess_board() -> list:
    """Create a standard chess board with pieces in starting position.
    Using Unicode chess symbols. Index 0 = rank 8 (black's side), Index 7 = rank 1 (white's side)."""
    return [
        ['♜', '♞', '♝', '♛', '♚', '♝', '♞', '♜'],  # Black pieces (rank 8)
        ['♟'] * 8,                                    # Black pawns (rank 7)
        [None] * 8,                                   # Rank 6
        [None] * 8,                                   # Rank 5
        [None] * 8,                                   # Rank 4
        [None] * 8,                                   # Rank 3
        ['♙'] * 8,                                    # White pawns (rank 2)
        ['♖', '♘', '♗', '♕', '♔', '♗', '♘', '♖'],  # White pieces (rank 1)
    ]


def build_chess_display(board: list, is_black_perspective: bool = False) -> str:
    """Build a chess board display from game state. Shows the board from the current player's perspective."""
    FILE_LABELS = ["a", "b", "c", "d", "e", "f", "g", "h"]
    RANK_LABELS = ["8", "7", "6", "5", "4", "3", "2", "1"]

    # Build file label line with dots on both sides
    files_to_show = list(reversed(FILE_LABELS)) if is_black_perspective else FILE_LABELS
    file_labels_str = " . ".join(files_to_show)
    file_line = f"... {file_labels_str} .\n"
    display = file_line

    # Determine which rows to iterate based on perspective
    if is_black_perspective:
        board_to_display = list(reversed(board))
        rank_labels_order = list(reversed(RANK_LABELS))
    else:
        board_to_display = board
        rank_labels_order = RANK_LABELS

    for rank_idx, row in enumerate(board_to_display):
        rank_num = rank_labels_order[rank_idx]

        line = f"{rank_num} "

        # Reverse row order for black perspective
        row_to_display = list(reversed(row)) if is_black_perspective else row

        for piece in row_to_display:
            if piece:
                line += piece + " "
            else:
                line += ".... "

        line += f"{rank_num}"
        display += line + "\n"

    display += file_line
    return display


def parse_chess_move(move_str: str) -> tuple[int, int, int, int] | None:
    """Parse chess move in algebraic notation (e.g., 'e2e4', 'e2 e4').
    Returns (from_row, from_col, to_row, to_col) or None if invalid."""
    move_str = move_str.lower().strip().replace(" ", "")

    if len(move_str) == 4:  # e2e4 format
        try:
            from_col = ord(move_str[0]) - ord('a')
            from_row = 8 - int(move_str[1])
            to_col = ord(move_str[2]) - ord('a')
            to_row = 8 - int(move_str[3])

            if all(0 <= x <= 7 for x in [from_row, from_col, to_row, to_col]):
                return (from_row, from_col, to_row, to_col)
        except (ValueError, IndexError):
            pass

    return None


def is_white_piece(piece: str | None) -> bool:
    """Check if piece is white (lowercase unicode symbols)."""
    if not piece:
        return False
    # White pieces: ♔♕♖♗♘♙
    return piece in '♔♕♖♗♘♙'


def is_black_piece(piece: str | None) -> bool:
    """Check if piece is black (uppercase unicode symbols)."""
    if not piece:
        return False
    # Black pieces: ♚♛♜♝♞♟
    return piece in '♚♛♜♝♞♟'


def is_valid_chess_move(board: list, from_r: int, from_c: int, to_r: int, to_c: int, is_white: bool) -> bool:
    """Basic chess move validation."""
    if not (0 <= from_r <= 7 and 0 <= from_c <= 7 and 0 <= to_r <= 7 and 0 <= to_c <= 7):
        return False

    piece = board[from_r][from_c]
    target = board[to_r][to_c]

    # Can't move empty square
    if not piece:
        return False

    # Check piece ownership
    if is_white and not is_white_piece(piece):
        return False
    if not is_white and not is_black_piece(piece):
        return False

    # Can't capture own pieces
    if target and ((is_white and is_white_piece(target)) or (not is_white and is_black_piece(target))):
        return False

    return True


class ChessCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="chess")
    async def cmd_chess(self, ctx: commands.Context, *args):
        # Special admin preview commands
        if args and args[0].lower() == "preview":
            if not is_admin(ctx):
                await ctx.send(embed=emb("❌ No Permission", "", C_RED))
                return
            preview_board = create_chess_board()
            await ctx.send(embed=emb("♟️ Chess Board Preview (White)", build_chess_display(preview_board, is_black_perspective=False), C_BLUE))
            return

        if args and args[0].lower() == "blackpreview":
            if not is_admin(ctx):
                await ctx.send(embed=emb("❌ No Permission", "", C_RED))
                return
            preview_board = create_chess_board()
            await ctx.send(embed=emb("♟️ Chess Board Preview (Black)", build_chess_display(preview_board, is_black_perspective=True), C_BLUE))
            return

        # Parse opponent and amount from args
        opponent = None
        amount = 0
        if ctx.message.mentions:
            opponent = ctx.message.mentions[0]
        if args:
            try:
                amount = int(args[-1])
            except (ValueError, IndexError):
                pass

        if await check_chess_channel(ctx):
            return

        cid = ctx.channel.id
        uid = ctx.author.id

        if cid in state.active_ttt_games or cid in state.active_c4_games or cid in state.active_chess_games:
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return

        if not await _setup_pvp_game(ctx, opponent, amount, "♟️ Chess Invite"):
            return

        state.active_chess_games[cid] = {
            "board": create_chess_board(),
            "players": [uid, opponent.id],  # [white, black]
            "current": uid,  # white moves first
            "moves": [],
            "amount": amount,
            "board_msg_id": None,
            "last_move": f"{ctx.author.display_name}'s turn (White)",
        }
        await _send_game_board(ctx, state.active_chess_games[cid], "♟️ Chess",
                               build_chess_display(state.active_chess_games[cid]["board"], is_black_perspective=False),
                               f"{ctx.author.mention} (White ♙)", f"{opponent.mention} (Black ♟)",
                               "Use `!move <e2e4>`", amount)
        await save_chess_games()

    @commands.command(name="move")
    async def cmd_move_chess(self, ctx: commands.Context, *args):
        cid = ctx.channel.id
        uid = ctx.author.id

        asyncio.create_task(_delete_after(ctx.message))

        if cid not in state.active_chess_games:
            err = await ctx.send("No active chess game in this channel. Start one with `!chess @user [amount]`")
            asyncio.create_task(_delete_after(err))
            return

        game = state.active_chess_games[cid]
        if uid != game["current"]:
            opponent_id = game["current"]
            opponent = ctx.guild.get_member(opponent_id) if ctx.guild else None
            err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {opponent.mention if opponent else 'opponent'}.", C_GOLD))
            asyncio.create_task(_delete_after(err))
            return

        if not args:
            err = await ctx.send("Usage: `!move <e2e4>` or `!move e2 e4` (from square to square in algebraic notation)")
            asyncio.create_task(_delete_after(err))
            return

        move = " ".join(args)

        parsed = parse_chess_move(move)
        if not parsed:
            err = await ctx.send("Invalid move format. Use algebraic notation like `e2e4`")
            asyncio.create_task(_delete_after(err))
            return

        from_r, from_c, to_r, to_c = parsed
        is_white = uid == game["players"][0]

        if not is_valid_chess_move(game["board"], from_r, from_c, to_r, to_c, is_white):
            err = await ctx.send("Invalid move. The piece can't move there or it's not your piece.")
            asyncio.create_task(_delete_after(err))
            return

        # Make the move
        board = game["board"]
        piece = board[from_r][from_c]
        board[to_r][to_c] = piece
        board[from_r][from_c] = None

        move_notation = f"{chr(ord('a') + from_c)}{8 - from_r}{chr(ord('a') + to_c)}{8 - to_r}"
        game["moves"].append(move_notation)

        # Switch turns
        game["current"] = game["players"][1] if uid == game["players"][0] else game["players"][0]
        next_player = ctx.guild.get_member(game["current"]) if ctx.guild else None

        game["last_move"] = f"{ctx.author.display_name} played {move_notation}"
        await save_chess_games()

        # Display from the next player's perspective
        is_black_perspective = game["current"] == game["players"][1]  # True if it's black's turn next
        await _edit_board(ctx.channel, game, emb("♟️ Chess", build_chess_display(board, is_black_perspective) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!move <e2e4>`\n\n**Last move:** {game['last_move']}", C_BLUE))


async def setup(bot):
    await bot.add_cog(ChessCog(bot))
