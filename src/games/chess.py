from __future__ import annotations

import asyncio
import datetime
import io
import logging

import chess
import chess.pgn
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_RED, C_GOLD, C_BLUE, C_GREEN, C_GREY,
    _delete_after, send_ephemeral, announce_record,
)
from src.economy import (
    add_balance, record_gambling_event,
)
from src.permissions import check_chess_channel, requires_perm
from src.guild_config import get_guild_cfg
from src.persistence import (
    save_chess_game, delete_chess_game, save_chess_report, load_chess_report,
    load_head_to_head, load_bot_head_to_head, count_pvp_wins_in_guild, try_set_record,
)
from src.games.ttt_c4 import _setup_pvp_game
from src.games import chess_engine, chess_render, chess_bot
from src.games.bot_chess_rewards import award_bot_defeat, RECORD_CATEGORY as _BOT_CHESS_RECORD
from src import state


BOARD_IMG_FILENAME = "board.png"


def _last_move_info_from_pgn(pgn_str: str) -> tuple[chess.Move | None, bool]:
    """Parse a PGN and return (last_move, was_capture). last_move is None
    if the game has no moves; was_capture is True iff the final move was
    a capture (including en passant) against the position immediately
    preceding it."""
    try:
        g = chess.pgn.read_game(io.StringIO(pgn_str))
    except Exception:
        return None, False
    if g is None:
        return None, False
    moves = list(g.mainline_moves())
    if not moves:
        return None, False
    # Replay all but the last move, then check capture status on the
    # pre-move board.
    board = g.board()
    for move in moves[:-1]:
        board.push(move)
    last_move = moves[-1]
    was_capture = board.is_capture(last_move)
    return last_move, was_capture


def _render_file_for_game(game: dict, *, orientation_for_uid: int | None = None) -> discord.File | None:
    try:
        board = chess_engine.board_from_fen(game["fen"])
    except Exception:
        return None
    orientation = chess.WHITE
    if orientation_for_uid is not None and orientation_for_uid == game.get("black_id"):
        orientation = chess.BLACK
    # FEN-derived boards have an empty move_stack, so board.peek() won't
    # work. Recover the last move + capture flag from the PGN.
    last_move, was_capture = _last_move_info_from_pgn(game.get("pgn", ""))
    try:
        png = chess_render.render_board_png(
            board, orientation=orientation, last_move=last_move,
            last_move_was_capture=was_capture,
        )
    except RuntimeError as e:
        logging.warning(f"chess render unavailable: {e}")
        return None
    return discord.File(io.BytesIO(png), filename=BOARD_IMG_FILENAME)


def _board_embed(title: str, description: str, color: int) -> discord.Embed:
    # Note: intentionally does NOT set_image on the attachment. The board PNG
    # is sent as a top-level attachment alongside this embed so it renders
    # outside the embed frame (larger inline display in Discord).
    return emb(title, description, color)


async def _bump_board(
    channel: discord.abc.Messageable, game: dict, embed: discord.Embed,
    *, file: discord.File | None = None,
):
    """Send a fresh embed+board pair, THEN delete the prior pair. The embed
    and image are sent as TWO separate messages (embed first, then board
    image) so the board renders BELOW the embed in Discord's UI — same-
    message attachments always render above their embed regardless of order.

    Order matters: post-then-delete (rather than delete-then-post) means the
    channel never has a window with no board visible. The user always sees
    either the old or the new board, never an empty channel between them.

    Tracks both message IDs:
      - game['embed_msg_id']: the text/embed message (sent first)
      - game['board_msg_id']: the image attachment message (sent second)
    """
    # Snapshot the prior IDs before we overwrite them with the new send.
    prior_ids = [game.get("board_msg_id"), game.get("embed_msg_id")]

    # Send the new pair first so the channel always has a current board.
    embed_msg = await channel.send(embed=embed)
    game["embed_msg_id"] = embed_msg.id
    if file is not None:
        image_msg = await channel.send(file=file)
        game["board_msg_id"] = image_msg.id
    else:
        # Render failed — no image; clear any stale board_msg_id so a future
        # bump doesn't try to delete a nonexistent message.
        game["board_msg_id"] = None

    # Now delete the prior pair (image first, then embed) so the most recent
    # message in the channel remains the new board.
    for prior_id in prior_ids:
        if prior_id is None:
            continue
        try:
            old = await channel.fetch_message(prior_id)
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


def _initial_pgn(white_name: str, black_name: str, guild_name: str | None,
                 *, starting_fen: str | None = None) -> str:
    game = chess.pgn.Game()
    if starting_fen is not None and starting_fen != chess_engine.STARTING_FEN:
        # SetUp + FEN headers let chess.pgn.read_game restore the position when
        # appending subsequent moves (otherwise _append_san_to_pgn parses moves
        # against the default starting board and silently rejects anything
        # illegal from that position).
        game.setup(chess.Board(fen=starting_fen))
    game.headers["Event"] = f"Discord chess ({guild_name})" if guild_name else "Discord chess"
    game.headers["Site"] = "Discord"
    game.headers["Date"] = datetime.date.today().strftime("%Y.%m.%d")
    game.headers["White"] = white_name
    game.headers["Black"] = black_name
    game.headers["Result"] = "*"
    return str(game)


def _movetext_only(pgn_str: str) -> str:
    """Strip PGN headers, return just the movetext (e.g. '1. e4 e5 2. Nf3 1-0').
    Falls back to the raw input if parsing fails."""
    try:
        g = chess.pgn.read_game(io.StringIO(pgn_str))
        if g is None:
            return pgn_str
        exporter = chess.pgn.StringExporter(headers=False, variations=False, comments=False)
        return g.accept(exporter)
    except Exception:
        return pgn_str


def _append_san_to_pgn(pgn_str: str, san: str) -> str:
    g = chess.pgn.read_game(io.StringIO(pgn_str))
    if g is None:
        return pgn_str
    board = g.end().board()
    try:
        move = board.parse_san(san)
    except Exception:
        return pgn_str
    g.end().add_variation(move)
    return str(g)


# Per-side starting counts in standard chess. Used to compute captured
# pieces as (initial - current) for the opponent's side.
_STARTING_PIECE_COUNTS = {
    chess.QUEEN: 1, chess.ROOK: 2, chess.BISHOP: 2,
    chess.KNIGHT: 2, chess.PAWN: 8,
}

# Render captured pieces using the OPPONENT'S color (i.e. their original
# color) since the line reads "pieces I took from you." Pawn glyphs included.
_PIECE_GLYPHS = {
    chess.WHITE: {
        chess.QUEEN: "♕", chess.ROOK: "♖", chess.BISHOP: "♗",
        chess.KNIGHT: "♘", chess.PAWN: "♙",
    },
    chess.BLACK: {
        chess.QUEEN: "♛", chess.ROOK: "♜", chess.BISHOP: "♝",
        chess.KNIGHT: "♞", chess.PAWN: "♟",
    },
}

# Display order for ties: queen > rook > bishop ≈ knight > pawn. Bishop
# and knight are both worth 3 but bishop sorts first by convention.
_PIECE_DISPLAY_ORDER = (chess.QUEEN, chess.ROOK, chess.BISHOP,
                        chess.KNIGHT, chess.PAWN)


def _captures_block(game: dict) -> str:
    """Two-line summary of each side's captures, formatted for the chess
    embed description. Empty string if neither side has captured anything.
    Leading newline included so callers can append directly."""
    try:
        board = chess_engine.board_from_fen(game["fen"])
    except Exception:
        return ""
    white_caps = _captures_summary(board, chess.WHITE)
    black_caps = _captures_summary(board, chess.BLACK)
    if not white_caps and not black_caps:
        return ""
    return f"\nWhite captured: {white_caps or '—'}\nBlack captured: {black_caps or '—'}"


def _captures_summary(board: chess.Board, captor_color: chess.Color) -> str:
    """Return a compact summary of pieces `captor_color` has taken from the
    opponent. Format: '<glyph1>,<glyph2>+N' where the two glyphs are the
    captor's two highest-value captures by piece type and N is the count
    of remaining captures. Empty string if no captures yet.

    Promotions inflate the captor's own piece counts but don't affect the
    opponent's, so they're correctly excluded from this calculation."""
    opp = not captor_color
    captured: list[chess.PieceType] = []
    for piece_type in _PIECE_DISPLAY_ORDER:
        remaining = len(board.pieces(piece_type, opp))
        taken = _STARTING_PIECE_COUNTS[piece_type] - remaining
        if taken > 0:
            captured.extend([piece_type] * taken)
    if not captured:
        return ""
    glyphs = _PIECE_GLYPHS[opp]
    top_two = captured[:2]
    head = ",".join(glyphs[pt] for pt in top_two)
    extra = len(captured) - len(top_two)
    return f"{head}+{extra}" if extra > 0 else head


def _player_display_name(guild: discord.Guild | None, uid: int, fallback: str) -> str:
    if guild is not None:
        m = guild.get_member(uid)
        if m is not None:
            return m.display_name
    return fallback


def _names_from_pgn(pgn_str: str) -> tuple[str | None, str | None]:
    """Extract the [White "..."] and [Black "..."] header values from a PGN.

    Returns (white_name, black_name); either may be None if the header is
    missing or unparseable. Used as the primary source of truth for player
    names at game-end since guild.get_member can miss on the privileged
    members intent.
    """
    white = black = None
    for line in pgn_str.splitlines():
        if line.startswith('[White "') and line.endswith('"]'):
            white = line[len('[White "'):-len('"]')]
        elif line.startswith('[Black "') and line.endswith('"]'):
            black = line[len('[Black "'):-len('"]')]
        if white is not None and black is not None:
            break
    return white, black


class ChessCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def resume_pending_bot_turns(self):
        """Scan loaded chess games for any where it's the bot's turn and
        schedule the bot's reply. Called from on_ready after init_db_state
        so games that were mid-bot-turn at restart don't freeze waiting
        for a human move to nudge them.

        Each reply runs as a background task — we don't await them in serial
        so startup isn't blocked by N parallel Maia spawns."""
        bot_user = self.bot.user if self.bot is not None else None
        if bot_user is None:
            return
        resumed = 0
        for cid, game in list(state.active_chess_games.items()):
            if game.get("current_id") != bot_user.id:
                continue
            try:
                channel = await self.bot.fetch_channel(cid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logging.warning(f"chess: can't resume bot turn in channel {cid}: {e}")
                continue
            asyncio.create_task(self._play_bot_reply(channel, cid))
            resumed += 1
        if resumed:
            logging.info(f"chess: resumed {resumed} pending bot turn(s) after restart")

    # ── !chess: dispatch on subcommand ──────────────────────────────────────
    @commands.command(name="chess")
    async def cmd_chess(self, ctx: commands.Context, *args):
        if args and args[0].lower() == "view":
            await self._cmd_view(ctx, args[1:])
            return
        if args and args[0].lower() == "pgn":
            await self._cmd_pgn(ctx, args[1:])
            return
        if args and args[0].lower() in ("help", "?"):
            await self._cmd_help(ctx)
            return

        opponent = ctx.message.mentions[0] if ctx.message.mentions else None
        # Bare `!chess` with no mention and no parseable args → show the help
        # menu instead of trying to start a malformed game.
        if opponent is None and not args:
            await self._cmd_help(ctx)
            return
        trailing_int: int | None = None
        if args:
            try:
                trailing_int = int(args[-1])
            except (ValueError, IndexError):
                pass

        if await check_chess_channel(ctx):
            return

        cid = ctx.channel.id

        if (
            cid in state.active_ttt_games
            or cid in state.active_c4_games
            or cid in state.active_chess_games
        ):
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return

        bot_user = self.bot.user if self.bot is not None else None
        is_bot_game = (
            opponent is not None
            and bot_user is not None
            and opponent.id == bot_user.id
        )

        if is_bot_game:
            raw_elo = trailing_int if trailing_int is not None else chess_bot.ELO_DEFAULT
            if not (chess_bot.ELO_MIN <= raw_elo <= chess_bot.ELO_MAX):
                await ctx.send(embed=emb(
                    "❌ Invalid Elo",
                    f"Elo must be between {chess_bot.ELO_MIN} and {chess_bot.ELO_MAX}.",
                    C_RED,
                ))
                return
            elo = chess_bot.round_elo_to_bin(raw_elo)
            if elo != raw_elo:
                await ctx.send(f"✍️ Rounded Elo to **{elo}**.")
            await self._start_bot_chess(ctx, elo)
            return

        amount = trailing_int if trailing_int is not None else 0
        await self._start_pvp_chess(ctx, opponent, amount)

    async def _start_pvp_chess(self, ctx: commands.Context, opponent, amount: int):
        cid = ctx.channel.id
        uid = ctx.author.id

        if not await _setup_pvp_game(ctx, opponent, amount, "♟️ Chess Invite"):
            return

        white_id, black_id = uid, opponent.id
        white_name = ctx.author.display_name
        black_name = opponent.display_name
        guild_name = ctx.guild.name if ctx.guild else None

        game = {
            "fen": chess_engine.STARTING_FEN,
            "pgn": _initial_pgn(white_name, black_name, guild_name),
            "white_id": white_id,
            "black_id": black_id,
            "current_id": white_id,
            "amount": amount,
            "last_move": f"{white_name}'s turn (White)",
            "board_msg_id": None,
        }
        state.active_chess_games[cid] = game

        wager_info = f"\nWager: {amount:,} 🪙 each" if amount > 0 else ""
        desc = (
            f"{ctx.author.mention} (White ♙) vs {opponent.mention} (Black ♟){wager_info}\n"
            f"{ctx.author.mention}'s turn. Type your move (e.g. `e4`, `Nf3`, `O-O`, `e2e4`) or `!move <move>`\n\n"
            f"**Last move:** {game['last_move']}"
            f"{_captures_block(game)}"
        )
        file = _render_file_for_game(game, orientation_for_uid=white_id)
        # Two messages so the board image renders BELOW the embed in Discord
        # (same-message attachments always render above their embed).
        embed_msg = await ctx.send(embed=_board_embed("♟️ Chess", desc, C_BLUE))
        game["embed_msg_id"] = embed_msg.id
        if file is not None:
            image_msg = await ctx.send(file=file)
            game["board_msg_id"] = image_msg.id
        else:
            game["board_msg_id"] = None
        await save_chess_game(cid)

    async def _start_bot_chess(self, ctx: commands.Context, elo: int):
        """Bot-vs-human game: skip _setup_pvp_game entirely. No wagers, no
        confirmation, no opponent balance. Human always plays White (v1)."""
        cid = ctx.channel.id
        uid = ctx.author.id
        bot_user = self.bot.user

        white_id, black_id = uid, bot_user.id
        white_name = ctx.author.display_name
        black_name = chess_bot.engine_name_with_elo(elo)
        guild_name = ctx.guild.name if ctx.guild else None

        game = {
            "fen": chess_engine.STARTING_FEN,
            "pgn": _initial_pgn(white_name, black_name, guild_name),
            "white_id": white_id,
            "black_id": black_id,
            "current_id": white_id,
            "amount": 0,
            "elo": elo,
            "last_move": f"{white_name}'s turn (White)",
            "board_msg_id": None,
        }
        state.active_chess_games[cid] = game

        desc = (
            f"{ctx.author.mention} (White ♙) vs 🤖 **{black_name}** (Black ♟)\n"
            f"{ctx.author.mention}'s turn. Type your move (e.g. `e4`, `Nf3`, `O-O`, `e2e4`) or `!move <move>`\n\n"
            f"**Last move:** {game['last_move']}"
            f"{_captures_block(game)}"
        )
        file = _render_file_for_game(game, orientation_for_uid=white_id)
        # Two messages so the board image renders BELOW the embed in Discord
        # (same-message attachments always render above their embed).
        embed_msg = await ctx.send(embed=_board_embed("♟️ Chess", desc, C_BLUE))
        game["embed_msg_id"] = embed_msg.id
        if file is not None:
            image_msg = await ctx.send(file=file)
            game["board_msg_id"] = image_msg.id
        else:
            game["board_msg_id"] = None
        await save_chess_game(cid)

    # ── !chessbot [elo]: alias for !chess @TheBot [elo] ─────────────────────
    @commands.command(name="chessbot")
    async def cmd_chessbot(self, ctx: commands.Context, *args):
        if await check_chess_channel(ctx):
            return

        cid = ctx.channel.id
        if (
            cid in state.active_ttt_games
            or cid in state.active_c4_games
            or cid in state.active_chess_games
        ):
            await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
            return

        bot_user = self.bot.user if self.bot is not None else None
        if bot_user is None:
            await ctx.send(embed=emb("❌ Bot Not Ready", "Bot user not initialized; try again in a moment.", C_RED))
            return

        raw_elo = chess_bot.ELO_DEFAULT
        if args:
            try:
                raw_elo = int(args[0])
            except ValueError:
                await ctx.send(embed=emb(
                    "❌ Invalid Elo",
                    f"Usage: `!chessbot [elo]` where elo is {chess_bot.ELO_MIN}-{chess_bot.ELO_MAX}.",
                    C_RED,
                ))
                return
        if not (chess_bot.ELO_MIN <= raw_elo <= chess_bot.ELO_MAX):
            await ctx.send(embed=emb(
                "❌ Invalid Elo",
                f"Elo must be between {chess_bot.ELO_MIN} and {chess_bot.ELO_MAX}.",
                C_RED,
            ))
            return
        elo = chess_bot.round_elo_to_bin(raw_elo)
        if elo != raw_elo:
            await ctx.send(f"✍️ Rounded Elo to **{elo}**.")

        await self._start_bot_chess(ctx, elo)

    # ── !chessthreats: admin debug view of all hanging pieces ───────────────
    @commands.command(name="chessthreats")
    @requires_perm
    async def cmd_chessthreats(self, ctx: commands.Context):
        """Render the active game's board with a red glow on every square
        whose piece is SEE-hanging (either color). Debugging tool for the
        threat-awareness check."""
        cid = ctx.channel.id
        if cid not in state.active_chess_games:
            await ctx.send(embed=emb(
                "❌ No Game",
                "No active chess game in this channel.",
                C_RED,
            ))
            return
        game = state.active_chess_games[cid]
        try:
            board = chess_engine.board_from_fen(game["fen"])
        except Exception:
            await ctx.send(embed=emb("❌ Bad State", "Couldn't parse game FEN.", C_RED))
            return
        threats = (
            chess_bot.hanging_squares(board, chess.WHITE)
            | chess_bot.hanging_squares(board, chess.BLACK)
        )
        # Orient the board for the admin if they're a player in the game,
        # otherwise default to white's POV.
        orientation = chess.WHITE
        if ctx.author.id == game.get("black_id"):
            orientation = chess.BLACK
        try:
            png = chess_render.render_board_png(
                board,
                orientation=orientation,
                threat_squares=threats,
            )
        except RuntimeError as e:
            await ctx.send(embed=emb(
                "❌ Render Failed",
                f"Couldn't render board: {e}",
                C_RED,
            ))
            return
        file = discord.File(io.BytesIO(png), filename=BOARD_IMG_FILENAME)
        count = len(threats)
        desc = (
            f"**{count}** hanging piece{'s' if count != 1 else ''} on the board "
            f"(red glow = SEE-losing for the side whose piece it is)."
        )
        await ctx.send(embed=emb("♟️ Chess Threats", desc, C_GREY), file=file)

    # ── !chess view <report_id> ─────────────────────────────────────────────
    async def _cmd_view(self, ctx: commands.Context, args: tuple[str, ...]):
        if not args:
            await send_ephemeral(ctx, embed=emb("❌ Usage", "Use `!chess view <report_id>` to replay a finished game.", C_RED))
            return
        try:
            report_id = int(args[0])
        except ValueError:
            await send_ephemeral(ctx, embed=emb("❌ Invalid", "Report id must be a number.", C_RED))
            return
        report = await load_chess_report(report_id)
        if report is None:
            await send_ephemeral(ctx, embed=emb("❌ Not Found", f"No chess game with report id `{report_id}`.", C_RED))
            return

        winner_id = report["winner_id"]
        bot_user = self.bot.user if self.bot is not None else None
        if winner_id is None:
            outcome_line = f"Draw ({report['result']})"
        else:
            # Prefer PGN-header names (always accurate, including the engine
            # label "Sub-Maia/Maia/Stockfish (N Elo)" for bot wins) over
            # guild.get_member (often misses without the privileged members
            # intent).
            pgn_white, pgn_black = _names_from_pgn(report.get("pgn", ""))
            if bot_user is not None and winner_id == bot_user.id:
                # Bot won — use whichever PGN header corresponds to the bot.
                winner_name = (
                    pgn_black if winner_id == report["black_id"] else pgn_white
                ) or "Bot"
            else:
                if winner_id == report["white_id"] and pgn_white:
                    winner_name = pgn_white
                elif winner_id == report["black_id"] and pgn_black:
                    winner_name = pgn_black
                else:
                    winner_name = _player_display_name(ctx.guild, winner_id, str(winner_id))
            color_label = "White" if winner_id == report["white_id"] else "Black"
            outcome_line = f"{color_label} ({winner_name}) wins ({report['result']})"

        try:
            board = chess.Board(fen=report["final_fen"])
        except Exception:
            board = chess_engine.new_board()
        file = None
        try:
            png = chess_render.render_board_png(board, orientation=chess.WHITE)
            file = discord.File(io.BytesIO(png), filename=BOARD_IMG_FILENAME)
        except RuntimeError as e:
            logging.warning(f"chess render unavailable in view: {e}")

        movetext = _movetext_only(report["pgn"])
        pgn_block = f"```pgn\n{movetext}\n```"
        # Embeds cap description at 4096 chars; trim if needed.
        if len(pgn_block) > 3800:
            pgn_block = f"```pgn\n{movetext[:3600]}\n... (truncated)```"
        pgn_hint = f"\n*Full PGN: `!chess pgn {report_id}`*"
        desc = f"{outcome_line}\n\n{pgn_block}{pgn_hint}"
        e = emb(f"♟️ Chess Game #{report_id}", desc, C_GREY)
        if file is not None:
            # Attach as top-level file so the board renders outside the embed
            # frame (larger inline display).
            await send_ephemeral(ctx, embed=e, file=file)
        else:
            await send_ephemeral(ctx, embed=e)

    # ── !chess pgn <report_id>: full headered PGN for lichess import ─────────
    async def _cmd_pgn(self, ctx: commands.Context, args: tuple[str, ...]):
        if not args:
            await send_ephemeral(ctx, embed=emb("❌ Usage", "Use `!chess pgn <report_id>` to get the full headered PGN.", C_RED))
            return
        try:
            report_id = int(args[0])
        except ValueError:
            await send_ephemeral(ctx, embed=emb("❌ Invalid", "Report id must be a number.", C_RED))
            return
        report = await load_chess_report(report_id)
        if report is None:
            await send_ephemeral(ctx, embed=emb("❌ Not Found", f"No chess game with report id `{report_id}`.", C_RED))
            return

        pgn_block = f"```pgn\n{report['pgn']}\n```"
        if len(pgn_block) > 3900:
            # PGN too long for an embed code block; fall back to a file attachment.
            file = discord.File(io.BytesIO(report["pgn"].encode("utf-8")), filename=f"chess_{report_id}.pgn")
            await send_ephemeral(
                ctx,
                embed=emb(f"♟️ Chess Game #{report_id} — PGN", "Full PGN attached (too long for embed).", C_GREY),
                file=file,
            )
        else:
            await send_ephemeral(ctx, embed=emb(f"♟️ Chess Game #{report_id} — PGN", pgn_block, C_GREY))

    # ── !chess (no args) / !chess help: show the chess command menu ─────────
    async def _cmd_help(self, ctx: commands.Context):
        elo_lo, elo_hi, elo_default = chess_bot.ELO_MIN, chess_bot.ELO_MAX, chess_bot.ELO_DEFAULT
        from src.games.bot_chess_rewards import (
            COINS_PER_NEW_ELO,
            COINS_PER_NEW_ELO_LOW,
            LOW_ELO_THRESHOLD,
        )
        desc = (
            "**Start a game**\n"
            "`!chess @user [wager]` — Play another player. Optional wager in 🪙; winner takes the pot.\n"
            f"`!chessbot [elo]` (alias `!chess @TheBot [elo]`) — Play the bot. Elo `{elo_lo}`–`{elo_hi}`, default `{elo_default}`. "
            f"Elo 100-1000 uses Sub-Maia, 1100-1900 uses Maia, 2000+ uses Stockfish.\n"
            f"  • Beat the bot to earn **{COINS_PER_NEW_ELO_LOW} 🪙 per new Elo point under {LOW_ELO_THRESHOLD}**, "
            f"**{COINS_PER_NEW_ELO} 🪙 per new Elo point at/above {LOW_ELO_THRESHOLD}** "
            "(above your daily highwater; resets 5am CT).\n"
            "\n"
            "**Make a move**\n"
            "`!move <move>` — e.g. `!move e4`, `!move Nf3`, `!move e2e4`, `!move O-O`, `!move e7e8q` (promote).\n"
            "  • In a chess channel during your turn you can also just type the move directly — the bot will pick it up and delete your message.\n"
            "\n"
            "**End / forfeit**\n"
            "`!stop` — Forfeit the active game in this channel. Wager goes to the opponent.\n"
            "\n"
            "**Replay finished games**\n"
            "`!chess view <id>` — Show the game outcome, final position image, and movetext.\n"
            "`!chess pgn <id>` — Full headered PGN (paste into [lichess.org/paste](https://lichess.org/paste) for analysis).\n"
            "\n"
            "**Notation**\n"
            "SAN (`Nf3`, `O-O`, `Qxh4#`) or UCI (`g1f3`, `e1g1`, `d8h4`). Both work everywhere."
        )
        await send_ephemeral(ctx, embed=emb("♟️ Chess — Commands", desc, C_BLUE))

    # ── !move ───────────────────────────────────────────────────────────────
    @commands.command(name="move")
    async def cmd_move_chess(self, ctx: commands.Context, *args):
        cid = ctx.channel.id
        uid = ctx.author.id

        asyncio.create_task(_delete_after(ctx.message))

        if cid not in state.active_chess_games:
            err = await ctx.send("No active chess game in this channel. Start one with `!chess @user [amount]` (PvP) or `!chessbot [elo]` (vs the bot).")
            asyncio.create_task(_delete_after(err))
            return

        game = state.active_chess_games[cid]
        if uid != game["current_id"]:
            opponent_id = game["current_id"]
            opp = ctx.guild.get_member(opponent_id) if ctx.guild else None
            err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {opp.mention if opp else 'opponent'}.", C_GOLD))
            asyncio.create_task(_delete_after(err))
            return

        if not args:
            err = await ctx.send("Usage: `!move <move>` (e.g. `!move e4`, `!move Nf3`, `!move e2e4`). Or just type the move directly in a chess channel.")
            asyncio.create_task(_delete_after(err))
            return

        move_str = " ".join(args).strip()
        applied, err_msg = await self._apply_human_move(
            ctx.channel, ctx.guild, ctx.author, move_str,
        )
        if not applied and err_msg is not None:
            err = await ctx.send(embed=emb("❌ Invalid Move", err_msg, C_RED))
            asyncio.create_task(_delete_after(err))

    async def _apply_human_move(
        self, channel: discord.abc.Messageable, guild: discord.Guild | None,
        author, move_str: str,
    ) -> tuple[bool, str | None]:
        """Validate + apply a human move. Returns (applied, error_message).

        applied=False, err=str: parse/illegal — caller decides whether to surface.
        applied=False, err=None: state-precondition failure (no game, wrong turn).
        applied=True, err=None: move applied; board re-rendered; bot reply
            scheduled if applicable.
        """
        cid = channel.id
        uid = author.id

        if cid not in state.active_chess_games:
            return False, None
        game = state.active_chess_games[cid]
        if uid != game["current_id"]:
            return False, None

        board = chess_engine.board_from_fen(game["fen"])
        move, err_msg = chess_engine.try_move(board, move_str)
        if move is None:
            return False, err_msg or "Invalid move."

        # Gate-and-claim: flip current_id synchronously BEFORE any await.
        prior_current = game["current_id"]
        prior_fen = game["fen"]
        prior_pgn = game["pgn"]
        prior_last_move = game["last_move"]

        capture_phrase = chess_engine.describe_capture(board, move)
        san = chess_engine.push_with_san(board, move)
        new_fen = board.fen()
        new_pgn = _append_san_to_pgn(game["pgn"], san)
        mover_name = author.display_name

        opponent_id = game["black_id"] if uid == game["white_id"] else game["white_id"]

        last_move_text = f"{mover_name} played {san}"
        if capture_phrase:
            last_move_text += f" — {capture_phrase}"
        game["fen"] = new_fen
        game["pgn"] = new_pgn
        game["current_id"] = opponent_id
        game["last_move"] = last_move_text

        # Game-over detection before persistence — if the move ended the game we
        # delete the row and insert a report instead of upserting.
        result, reason = chess_engine.game_over_info(board)
        if result is not None:
            await self._finalize_game(channel, cid, game, board, result, reason, mover_name=mover_name, san=san)
            return True, None

        try:
            await save_chess_game(cid)
        except Exception as e:
            # Rollback state on save failure so the user can retry.
            game["fen"] = prior_fen
            game["pgn"] = prior_pgn
            game["current_id"] = prior_current
            game["last_move"] = prior_last_move
            logging.error(f"chess save_chess_game failed: {e}", exc_info=True)
            err = await channel.send(embed=emb("❌ Save Failed", "Couldn't save the move. Try again.", C_RED))
            asyncio.create_task(_delete_after(err))
            return False, None

        await self._render_and_bump_after_move(channel, cid, game, board, opponent_id)

        # If the next player is the bot, fire Stockfish's reply as a background
        # task so this !move handler returns promptly.
        bot_user = self.bot.user if self.bot is not None else None
        if bot_user is not None and opponent_id == bot_user.id:
            asyncio.create_task(self._play_bot_reply(channel, cid))
        return True, None

    # ── bare-move listener: accept `e4` as shorthand for `!move e4` ──────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Fast-path bail conditions — order is critical for perf since every
        # channel message hits this listener.
        if message.author.bot:
            return
        if not message.guild:
            return
        content = (message.content or "").strip()
        if not content or content.startswith("!") or len(content) > 10:
            return
        game = state.active_chess_games.get(message.channel.id)
        if game is None or message.author.id != game.get("current_id"):
            return
        # Restrict to configured chess channels only — without this, plain text
        # like "e4" in any channel with an active game would consume chat.
        cfg = get_guild_cfg(message.guild.id) or {}
        chess_channels = cfg.get("chess_channels", []) or cfg.get("game_channels", [])
        if chess_channels and message.channel.id not in chess_channels:
            return

        board = chess_engine.board_from_fen(game["fen"])
        move, _err = chess_engine.try_move(board, content)
        if move is None:
            return  # silently ignore non-move text

        # Delete the trigger message before applying so the channel stays clean
        # even if the apply takes a moment (board render + DB save).
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        await self._apply_human_move(
            message.channel, message.guild, message.author, content,
        )

    async def _render_and_bump_after_move(
        self, channel: discord.abc.Messageable, cid: int, game: dict,
        board: chess.Board, opponent_id: int,
    ):
        """Render the new board, bump it to the bottom, persist the new msg id."""
        file = _render_file_for_game(game, orientation_for_uid=opponent_id)
        guild = channel.guild if hasattr(channel, "guild") else None
        next_player = guild.get_member(opponent_id) if guild is not None else None
        bot_user = self.bot.user if self.bot is not None else None
        if bot_user is not None and opponent_id == bot_user.id:
            elo = game.get("elo", chess_bot.ELO_DEFAULT)
            next_mention = f"🤖 **{chess_bot.engine_name_with_elo(elo)}**"
        else:
            next_mention = next_player.mention if next_player else "Next player"
        check_note = " — **check!**" if board.is_check() else ""
        desc = (
            f"{next_mention}'s turn{check_note}. Type your move (e.g. `e4`, `Nf3`, `O-O`, `e2e4`) or `!move <move>`\n\n"
            f"**Last move:** {game['last_move']}"
            f"{_captures_block(game)}"
        )
        if file is not None:
            await _bump_board(channel, game, _board_embed("♟️ Chess", desc, C_BLUE), file=file)
        else:
            await _bump_board(channel, game, emb("♟️ Chess", desc, C_BLUE))
        # Bumping reassigned board_msg_id; persist so a restart hits the right message.
        try:
            await save_chess_game(cid)
        except Exception as e:
            logging.error(f"chess save_chess_game (bump persist) failed: {e}", exc_info=True)

    async def _play_bot_reply(self, channel: discord.abc.Messageable, cid: int):
        """Stockfish plays the next move. Runs as a background task so the
        user's !move handler returns immediately."""
        game = state.active_chess_games.get(cid)
        if game is None:
            # User resigned via !stop between their move and ours.
            return
        bot_user = self.bot.user if self.bot is not None else None
        if bot_user is None or game.get("current_id") != bot_user.id:
            return

        elo = game.get("elo", chess_bot.ELO_DEFAULT)
        bot_name = chess_bot.engine_name_with_elo(elo)

        try:
            move = await chess_bot.pick_move(game["fen"], elo)
        except Exception as e:
            logging.error(f"chess engine pick_move failed: {e}", exc_info=True)
            await channel.send(embed=emb(
                "❌ Chess Engine Error",
                "The chess engine failed to respond. Use `!stop` to end the game.",
                C_RED,
            ))
            return

        # Re-fetch in case state changed during pick_move (~500ms+).
        game = state.active_chess_games.get(cid)
        if game is None or game.get("current_id") != bot_user.id:
            return

        board = chess_engine.board_from_fen(game["fen"])
        if move not in board.legal_moves:
            # Stockfish returned a move that doesn't apply to current FEN. State drifted.
            logging.error(f"stockfish move {move} illegal for fen {game['fen']}")
            return

        capture_phrase = chess_engine.describe_capture(board, move)
        san = chess_engine.push_with_san(board, move)
        game["fen"] = board.fen()
        game["pgn"] = _append_san_to_pgn(game["pgn"], san)
        game["current_id"] = game["white_id"]  # bot is always black in v1
        last_move_text = f"{bot_name} played {san}"
        if capture_phrase:
            last_move_text += f" — {capture_phrase}"
        game["last_move"] = last_move_text

        result, reason = chess_engine.game_over_info(board)
        if result is not None:
            await self._finalize_game(channel, cid, game, board, result, reason, mover_name=bot_name, san=san)
            return

        try:
            await save_chess_game(cid)
        except Exception as e:
            logging.error(f"chess save_chess_game (bot reply) failed: {e}", exc_info=True)
            # Don't roll back — the move is legal and applied; we just lost the save.
            # Next user move will save the combined state.

        await self._render_and_bump_after_move(channel, cid, game, board, game["white_id"])

    async def _finalize_game(
        self, channel: discord.abc.Messageable, cid: int, game: dict,
        board: chess.Board, result: str, reason: str | None,
        *, mover_name: str, san: str,
    ):
        # Patch PGN's Result header to the final outcome before persisting the report.
        final_pgn = game["pgn"].replace('[Result "*"]', f'[Result "{result}"]')
        game["pgn"] = final_pgn

        winner_color = chess_engine.winner_color(board)
        if winner_color is None:
            winner_id = None
        else:
            winner_id = game["white_id"] if winner_color else game["black_id"]

        amount = int(game.get("amount", 0))
        guild = channel.guild if hasattr(channel, "guild") else None
        gid = guild.id if guild is not None else None
        bot_user = self.bot.user if self.bot is not None else None

        payout_line = ""
        if amount > 0:
            if winner_id is None:
                # Draw — refund both players.
                await add_balance(game["white_id"], amount)
                await add_balance(game["black_id"], amount)
                payout_line = f" Both players get {amount:,} 🪙 back."
            else:
                winnings = amount * 2
                await add_balance(winner_id, winnings)
                loser_id = game["black_id"] if winner_id == game["white_id"] else game["white_id"]
                await record_gambling_event(gid, winner_id, gained=amount)
                await record_gambling_event(gid, loser_id, lost=amount)
                payout_line = f" Winner takes {winnings:,} 🪙."

        try:
            report_id = await save_chess_report(
                guild_id=gid,
                channel_id=cid,
                white_id=game["white_id"],
                black_id=game["black_id"],
                winner_id=winner_id,
                result=result,
                pgn=final_pgn,
                final_fen=board.fen(),
                elo=game.get("elo"),
            )
        except Exception as e:
            logging.error(f"chess save_chess_report failed: {e}", exc_info=True)
            report_id = None

        try:
            await delete_chess_game(cid)
        except Exception as e:
            logging.error(f"chess delete_chess_game failed: {e}", exc_info=True)
        state.active_chess_games.pop(cid, None)

        if winner_id is None:
            headline = f"Draw by {reason}." if reason else "Draw."
            color = C_GOLD
            winner_name = None
        else:
            if bot_user is not None and winner_id == bot_user.id:
                # Bot winner — guild.get_member often returns None for the bot
                # itself, so use the engine-and-Elo label instead of the raw uid.
                elo = game.get("elo", chess_bot.ELO_DEFAULT)
                winner_name = chess_bot.engine_name_with_elo(elo)
            else:
                # Prefer the name embedded in the PGN headers at game-start
                # (always accurate) over guild.get_member (often misses without
                # the privileged members intent).
                pgn_white, pgn_black = _names_from_pgn(game.get("pgn", ""))
                if winner_id == game["white_id"] and pgn_white:
                    winner_name = pgn_white
                elif winner_id == game["black_id"] and pgn_black:
                    winner_name = pgn_black
                else:
                    winner_name = _player_display_name(guild, winner_id, str(winner_id))
            headline = f"{winner_name} wins by {reason}." if reason else f"{winner_name} wins."
            color = C_GREEN

            # Bot-defeat bounty: when a human beats Stockfish, pay out per new
            # Elo point gained today over the user's daily highwater (split rate
            # — see bot_chess_rewards._payout_for_range).
            # No-ops when winner is the bot, when it's PvP (no "elo" key), or
            # when this elo doesn't exceed today's highwater.
            if (
                bot_user is not None
                and winner_id != bot_user.id
                and "elo" in game
            ):
                bot_elo = int(game.get("elo", 0) or 0)
                try:
                    bounty, record_broken = await award_bot_defeat(
                        user_id=winner_id, guild_id=gid,
                        holder_name=winner_name, bot_elo=bot_elo,
                    )
                    if bounty > 0:
                        payout_line += f" **+{bounty:,} 🪙** for defeating a {bot_elo}-Elo bot today."
                    if record_broken:
                        asyncio.create_task(
                            announce_record(channel, _BOT_CHESS_RECORD, winner_name, bot_elo)
                        )
                except Exception as e:
                    logging.error(f"bot_chess_rewards.award_bot_defeat failed: {e}", exc_info=True)

        # Head-to-head line. PvP uses the all-time pairwise record; bot games
        # use the per-Elo record so a 0-3 vs Sub-Maia 400 doesn't pollute the
        # 5-1 vs Maia 1500 record.
        h2h_line = ""
        is_pvp = "elo" not in game and (
            bot_user is None
            or (game["white_id"] != bot_user.id and game["black_id"] != bot_user.id)
        )
        if is_pvp:
            try:
                white_id = game["white_id"]
                black_id = game["black_id"]
                h2h = await load_head_to_head(white_id, black_id)
                pgn_white, pgn_black = _names_from_pgn(game.get("pgn", ""))
                white_name = pgn_white or _player_display_name(guild, white_id, str(white_id))
                black_name = pgn_black or _player_display_name(guild, black_id, str(black_id))
                h2h_line = (
                    f"\n\n**Head-to-head:** {white_name} {h2h['wins_a']} – "
                    f"{h2h['wins_b']} {black_name}"
                    + (f" ({h2h['draws']} draws)" if h2h['draws'] else "")
                )
            except Exception as e:
                logging.error(f"chess head-to-head load failed: {e}", exc_info=True)

            # chess_pvp_wins record: count this user's all-time PvP wins in
            # this guild and try to set the record. Excludes bot games.
            if winner_id is not None and gid is not None and bot_user is not None:
                try:
                    wins = await count_pvp_wins_in_guild(winner_id, gid, bot_user.id)
                    set_new = await try_set_record(
                        gid, "chess_pvp_wins", wins, winner_id, winner_name,
                    )
                    if set_new:
                        asyncio.create_task(
                            announce_record(channel, "chess_pvp_wins", winner_name, wins)
                        )
                except Exception as e:
                    logging.error(f"chess_pvp_wins record update failed: {e}", exc_info=True)
        elif bot_user is not None and "elo" in game:
            # Bot game: show user's per-Elo record vs the bot.
            try:
                bot_elo = game["elo"]
                human_id = (
                    game["white_id"] if game["white_id"] != bot_user.id
                    else game["black_id"]
                )
                pgn_white, pgn_black = _names_from_pgn(game.get("pgn", ""))
                human_name = (
                    pgn_white if human_id == game["white_id"] else pgn_black
                ) or _player_display_name(guild, human_id, str(human_id))
                bot_label = chess_bot.engine_name_with_elo(bot_elo)
                bot_h2h = await load_bot_head_to_head(human_id, bot_user.id, bot_elo)
                h2h_line = (
                    f"\n\n**vs {bot_label}:** {human_name} "
                    f"{bot_h2h['wins']} – {bot_h2h['losses']}"
                    + (f" ({bot_h2h['draws']} draws)" if bot_h2h['draws'] else "")
                )
            except Exception as e:
                logging.error(f"chess bot head-to-head load failed: {e}", exc_info=True)

        view_line = f"\n\nView this game: `!chess view {report_id}`" if report_id is not None else ""
        desc = (
            f"{mover_name} played **{san}**.\n\n"
            f"**{headline}**{payout_line}"
            f"{h2h_line}"
            f"{_captures_block(game)}"
            f"{view_line}"
        )

        file = _render_file_for_game(game, orientation_for_uid=None)
        if file is not None:
            await _bump_board(channel, game, _board_embed("♟️ Chess — Game Over", desc, color), file=file)
        else:
            await _bump_board(channel, game, emb("♟️ Chess — Game Over", desc, color))


async def setup(bot):
    await bot.add_cog(ChessCog(bot))
