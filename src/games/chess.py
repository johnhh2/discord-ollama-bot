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
    _delete_after, send_ephemeral,
)
from src.economy import (
    add_balance, record_gambling_event,
)
from src.permissions import check_chess_channel
from src.persistence import (
    save_chess_game, delete_chess_game, save_chess_report, load_chess_report,
)
from src.games.ttt_c4 import _setup_pvp_game
from src.games import chess_engine, chess_render, chess_bot
from src import state


BOARD_IMG_FILENAME = "board.png"


def _render_file_for_game(game: dict, *, orientation_for_uid: int | None = None) -> discord.File | None:
    try:
        board = chess_engine.board_from_fen(game["fen"])
    except Exception:
        return None
    orientation = chess.WHITE
    if orientation_for_uid is not None and orientation_for_uid == game.get("black_id"):
        orientation = chess.BLACK
    last_move = None
    if board.move_stack:
        last_move = board.peek()
    try:
        png = chess_render.render_board_png(
            board, orientation=orientation, last_move=last_move,
        )
    except RuntimeError as e:
        logging.warning(f"chess render unavailable: {e}")
        return None
    return discord.File(io.BytesIO(png), filename=BOARD_IMG_FILENAME)


def _board_embed(title: str, description: str, color: int) -> discord.Embed:
    e = emb(title, description, color)
    e.set_image(url=f"attachment://{BOARD_IMG_FILENAME}")
    return e


async def _bump_board(
    channel: discord.abc.Messageable, game: dict, embed: discord.Embed,
    *, file: discord.File | None = None,
):
    """Delete the previous board message and send a fresh one so the board
    is always the most recent message in the channel. Updates game['board_msg_id']
    to the new message id."""
    prior_id = game.get("board_msg_id")
    if prior_id is not None:
        try:
            old = await channel.fetch_message(prior_id)
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    send_kwargs: dict = {"embed": embed}
    if file is not None:
        send_kwargs["file"] = file
    msg = await channel.send(**send_kwargs)
    game["board_msg_id"] = msg.id


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


def _player_display_name(guild: discord.Guild | None, uid: int, fallback: str) -> str:
    if guild is not None:
        m = guild.get_member(uid)
        if m is not None:
            return m.display_name
    return fallback


class ChessCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── !chess: dispatch on subcommand ──────────────────────────────────────
    @commands.command(name="chess")
    async def cmd_chess(self, ctx: commands.Context, *args):
        if args and args[0].lower() == "view":
            await self._cmd_view(ctx, args[1:])
            return

        opponent = ctx.message.mentions[0] if ctx.message.mentions else None
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
            elo = trailing_int if trailing_int is not None else chess_bot.ELO_DEFAULT
            if not (chess_bot.ELO_MIN <= elo <= chess_bot.ELO_MAX):
                await ctx.send(embed=emb(
                    "❌ Invalid Elo",
                    f"Elo must be between {chess_bot.ELO_MIN} and {chess_bot.ELO_MAX}.",
                    C_RED,
                ))
                return
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
            f"{ctx.author.mention}'s turn. Use `!move <e2e4>` or SAN (`Nf3`)\n\n"
            f"**Last move:** {game['last_move']}"
        )
        file = _render_file_for_game(game, orientation_for_uid=white_id)
        send_kwargs: dict = {"embed": _board_embed("♟️ Chess", desc, C_BLUE)}
        if file is not None:
            send_kwargs["file"] = file
        else:
            send_kwargs["embed"] = emb("♟️ Chess", desc, C_BLUE)
        msg = await ctx.send(**send_kwargs)
        game["board_msg_id"] = msg.id
        await save_chess_game(cid)

    async def _start_bot_chess(self, ctx: commands.Context, elo: int):
        """Bot-vs-human game: skip _setup_pvp_game entirely. No wagers, no
        confirmation, no opponent balance. Human always plays White (v1)."""
        cid = ctx.channel.id
        uid = ctx.author.id
        bot_user = self.bot.user

        white_id, black_id = uid, bot_user.id
        white_name = ctx.author.display_name
        black_name = f"Stockfish ({elo} Elo)"
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
            f"{ctx.author.mention}'s turn. Use `!move <e2e4>` or SAN (`Nf3`)\n\n"
            f"**Last move:** {game['last_move']}"
        )
        file = _render_file_for_game(game, orientation_for_uid=white_id)
        send_kwargs: dict = {"embed": _board_embed("♟️ Chess", desc, C_BLUE)}
        if file is not None:
            send_kwargs["file"] = file
        else:
            send_kwargs["embed"] = emb("♟️ Chess", desc, C_BLUE)
        msg = await ctx.send(**send_kwargs)
        game["board_msg_id"] = msg.id
        await save_chess_game(cid)

    # ── !chess view <report_id> ─────────────────────────────────────────────
    async def _cmd_view(self, ctx: commands.Context, args: tuple[str, ...]):
        if not args:
            await send_ephemeral(ctx, embed=emb("❌ Usage", "Use `!chess view <report_id>`", C_RED))
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
        if winner_id is None:
            outcome_line = f"Draw ({report['result']})"
        elif winner_id == report["white_id"]:
            outcome_line = f"White ({_player_display_name(ctx.guild, winner_id, str(winner_id))}) wins ({report['result']})"
        else:
            outcome_line = f"Black ({_player_display_name(ctx.guild, winner_id, str(winner_id))}) wins ({report['result']})"

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

        pgn_block = f"```pgn\n{report['pgn']}\n```"
        # Embeds cap description at 4096 chars; trim PGN if needed.
        if len(pgn_block) > 3800:
            pgn_block = f"```pgn\n{report['pgn'][:3600]}\n... (truncated)```"
        desc = f"{outcome_line}\n\n{pgn_block}"
        e = emb(f"♟️ Chess Game #{report_id}", desc, C_GREY)
        if file is not None:
            e.set_image(url=f"attachment://{BOARD_IMG_FILENAME}")
            await send_ephemeral(ctx, embed=e, file=file)
        else:
            await send_ephemeral(ctx, embed=e)

    # ── !move ───────────────────────────────────────────────────────────────
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
        if uid != game["current_id"]:
            opponent_id = game["current_id"]
            opp = ctx.guild.get_member(opponent_id) if ctx.guild else None
            err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {opp.mention if opp else 'opponent'}.", C_GOLD))
            asyncio.create_task(_delete_after(err))
            return

        if not args:
            err = await ctx.send("Usage: `!move <e2e4>` or `!move Nf3`")
            asyncio.create_task(_delete_after(err))
            return

        move_str = " ".join(args).strip()

        # Parse + validate against the live board built from FEN.
        board = chess_engine.board_from_fen(game["fen"])
        move, err_msg = chess_engine.try_move(board, move_str)
        if move is None:
            err = await ctx.send(embed=emb("❌ Invalid Move", err_msg or "Invalid move.", C_RED))
            asyncio.create_task(_delete_after(err))
            return

        # Gate-and-claim: flip current_id synchronously BEFORE any await so racing !move
        # invocations bail at the gate above. Roll back only if the save fails.
        prior_current = game["current_id"]
        prior_fen = game["fen"]
        prior_pgn = game["pgn"]
        prior_last_move = game["last_move"]

        san = chess_engine.push_with_san(board, move)
        new_fen = board.fen()
        new_pgn = _append_san_to_pgn(game["pgn"], san)
        mover_name = ctx.author.display_name

        opponent_id = game["black_id"] if uid == game["white_id"] else game["white_id"]

        game["fen"] = new_fen
        game["pgn"] = new_pgn
        game["current_id"] = opponent_id
        game["last_move"] = f"{mover_name} played {san}"

        # Game-over detection before persistence — if the move ended the game we
        # delete the row and insert a report instead of upserting.
        result, reason = chess_engine.game_over_info(board)
        if result is not None:
            await self._finalize_game(ctx.channel, cid, game, board, result, reason, mover_name=mover_name, san=san)
            return

        try:
            await save_chess_game(cid)
        except Exception as e:
            # Rollback state on save failure so the user can retry.
            game["fen"] = prior_fen
            game["pgn"] = prior_pgn
            game["current_id"] = prior_current
            game["last_move"] = prior_last_move
            logging.error(f"chess save_chess_game failed: {e}", exc_info=True)
            err = await ctx.send(embed=emb("❌ Save Failed", "Couldn't save the move. Try again.", C_RED))
            asyncio.create_task(_delete_after(err))
            return

        await self._render_and_bump_after_move(ctx.channel, cid, game, board, opponent_id)

        # If the next player is the bot, fire Stockfish's reply as a background
        # task so this !move handler returns promptly.
        bot_user = self.bot.user if self.bot is not None else None
        if bot_user is not None and opponent_id == bot_user.id:
            asyncio.create_task(self._play_bot_reply(ctx.channel, cid))

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
            next_mention = f"🤖 Stockfish ({elo} Elo)"
        else:
            next_mention = next_player.mention if next_player else "Next player"
        check_note = " — **check!**" if board.is_check() else ""
        desc = (
            f"{next_mention}'s turn{check_note}. Use `!move <e2e4>` or SAN (`Nf3`)\n\n"
            f"**Last move:** {game['last_move']}"
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
        bot_name = f"Stockfish ({elo} Elo)"

        try:
            move = await chess_bot.pick_move(game["fen"], elo)
        except Exception as e:
            logging.error(f"stockfish pick_move failed: {e}", exc_info=True)
            await channel.send(embed=emb(
                "❌ Stockfish Error",
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

        san = chess_engine.push_with_san(board, move)
        game["fen"] = board.fen()
        game["pgn"] = _append_san_to_pgn(game["pgn"], san)
        game["current_id"] = game["white_id"]  # bot is always black in v1
        game["last_move"] = f"{bot_name} played {san}"

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
        else:
            winner_name = _player_display_name(guild, winner_id, str(winner_id))
            headline = f"{winner_name} wins by {reason}." if reason else f"{winner_name} wins."
            color = C_GREEN

        view_line = f"\n\nView this game: `!chess view {report_id}`" if report_id is not None else ""
        desc = (
            f"{mover_name} played **{san}**.\n\n"
            f"**{headline}**{payout_line}"
            f"{view_line}"
        )

        file = _render_file_for_game(game, orientation_for_uid=None)
        if file is not None:
            await _bump_board(channel, game, _board_embed("♟️ Chess — Game Over", desc, color), file=file)
        else:
            await _bump_board(channel, game, emb("♟️ Chess — Game Over", desc, color))


async def setup(bot):
    await bot.add_cog(ChessCog(bot))
