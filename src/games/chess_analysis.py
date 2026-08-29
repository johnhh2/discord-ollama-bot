"""Post-game engine analysis: per-player strength estimates + cheat flagging.

After every finished chess game long enough to analyze (bot AND PvP),
_finalize_game schedules analyze_and_post as a background task. It replays
the PGN through full-strength Stockfish, computes per-player accuracy stats
— average centipawn loss (ACPL) and top-engine-move match rate over
non-trivial positions — maps ACPL to a rough performance-Elo estimate,
appends the results to the game-over embed (edit-in-place, fallback to a
follow-up embed), and persists them on the chess_reports row so
`!chess view` shows them forever after.

Cheat flagging: a human who beats (or draws) a FLAG_MIN_BOT_ELO+ bot while
playing at near-engine accuracy over enough non-trivial moves gets the game
flagged. An alert embed goes to the global admin-log channel
(`!settings-channel admin-log`) with the stats and the user's all-time
flagged-game count. This is a heuristic tripwire for a human to review, NOT
proof — thresholds are tuned to catch sustained master-level play, which the
false-positive traps below can't reach:
  - the opening is skipped (memorized theory looks engine-perfect),
  - "trivial" positions (only-move, or best move crushes the second-best)
    are excluded from the match rate — punishing a weak bot's blunders is
    exactly where honest humans look engine-like,
  - a minimum count of non-trivial analyzed moves is required, so short
    games can't trip it.

Engine cost is bounded: one Stockfish process per finished game, one
analyse call per post-book position at depth ANALYSIS_DEPTH with a
per-position wall-clock cap, run strictly after the game ends. The
engine-facing seam is _run_engine_analysis; everything below it is pure
math and unit-testable without a Stockfish binary.
"""
from __future__ import annotations

import io
import logging
import os
import shutil

import chess
import chess.engine
import chess.pgn
import discord

from src import state
from src.games.chess_bot import _resolve_stockfish_path
from src.helpers import emb, C_GREY, C_RED
from src.persistence import count_flagged_reports, save_chess_analysis


# Bumped when the stat definitions change, so stored analyses from different
# eras aren't compared apples-to-oranges.
ANALYSIS_VERSION = 1

# Per-position engine budget. Depth 12 is plenty to grade human/bot moves;
# the time cap keeps a pathological position from stalling the whole pass.
# Worst case for a long game: ~80 positions * 0.4s = ~32s of background CPU.
ANALYSIS_DEPTH = 12
ANALYSIS_TIME_PER_POSITION = 0.4
ANALYSIS_MULTIPV = 2  # best + runner-up, for the triviality gap below

# The first BOOK_PLIES half-moves are skipped: opening theory is memorized,
# so grading it inflates everyone's accuracy.
BOOK_PLIES = 12

# A position is "trivial" when the best move beats the runner-up by more
# than this many centipawns (obvious recapture, mate-in-sight, only path) —
# or when there's literally one legal move. Trivial positions still count
# toward ACPL but not toward the match rate.
TRIVIAL_GAP_CP = 200

# Eval clamp: beyond ±10 pawns the game is decided and centipawn deltas are
# engine noise; capping keeps one blown position from dominating ACPL.
CP_CAP = 1000
MATE_SCORE_CP = 2000  # mate mapped to a large cp value, then clamped

# Games shorter than this many plies aren't analyzed at all — too little
# post-book signal to say anything about either player.
MIN_PLIES_FOR_ANALYSIS = 24

# ── Cheat-flag thresholds (bot games only; human won or drew) ────────────────
FLAG_MIN_BOT_ELO = 1100
FLAG_MAX_ACPL = 25.0
FLAG_MIN_MATCH_PCT = 70.0
FLAG_MIN_NONTRIVIAL = 15

# ACPL → performance-Elo anchors (piecewise linear, rough fit to published
# online-play accuracy curves). This is an ESTIMATE for display and flagging
# only — label it "~" wherever it's shown. Clamped to the ends, rounded to
# the nearest 50 to avoid false precision.
_ACPL_ELO_ANCHORS: list[tuple[float, int]] = [
    (5, 2900),
    (10, 2650),
    (15, 2500),
    (20, 2350),
    (30, 2100),
    (40, 1900),
    (50, 1750),
    (70, 1500),
    (90, 1300),
    (120, 1050),
    (160, 800),
    (250, 500),
]


def estimate_elo_from_acpl(acpl: float) -> int:
    """Rough performance-Elo estimate from average centipawn loss."""
    anchors = _ACPL_ELO_ANCHORS
    if acpl <= anchors[0][0]:
        return anchors[0][1]
    if acpl >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= acpl <= x1:
            frac = (acpl - x0) / (x1 - x0)
            elo = y0 + frac * (y1 - y0)
            return int(round(elo / 50.0)) * 50
    return anchors[-1][1]


def engine_available() -> bool:
    """True if a Stockfish binary is resolvable — the finalize hook checks
    this synchronously so no analysis task is even created without one."""
    path = _resolve_stockfish_path()
    return shutil.which(path) is not None or os.path.exists(path)


async def _eval_position(board: chess.Board, evaluate):
    """(best_move, best_cp, second_cp) from the side-to-move's POV.
    Terminal positions short-circuit without an engine call: checkmate is
    the cap against the mated side, any other game-over is a dead draw."""
    if board.is_game_over(claim_draw=False):
        if board.is_checkmate():
            return None, -CP_CAP, None
        return None, 0, None
    return await evaluate(board)


async def _collect_move_evals(pgn_str: str, evaluate) -> list[dict] | None:
    """Replay a PGN and grade every post-book half-move.

    `evaluate(board)` → (best_move, best_cp, second_cp), cp from the
    side-to-move's POV — injected so tests can grade without an engine.

    Each returned row: {"color", "matched", "loss", "trivial"}. Each
    position is evaluated once; the eval of the position AFTER a move
    (negated to the mover's POV) is the played move's value, so
    loss = best_cp − played_cp.
    """
    game = chess.pgn.read_game(io.StringIO(pgn_str))
    if game is None:
        return None
    moves = list(game.mainline_moves())
    if len(moves) < MIN_PLIES_FOR_ANALYSIS:
        return None

    board = game.board()
    evals: list[dict] = []
    prev = None  # eval of the current position, carried between plies
    for i, move in enumerate(moves):
        if i < BOOK_PLIES:
            board.push(move)
            continue
        if prev is None:
            prev = await _eval_position(board, evaluate)
        best_move, best_cp, second_cp = prev
        only_move = board.legal_moves.count() == 1
        mover_is_white = board.turn == chess.WHITE
        board.push(move)
        nxt = await _eval_position(board, evaluate)
        prev = nxt
        if best_cp is None or nxt[1] is None:
            continue  # engine gave no score for one side of the delta
        played_cp = -nxt[1]  # opponent POV → mover POV
        loss = min(max(0, best_cp - played_cp), CP_CAP)
        trivial = (
            only_move
            or second_cp is None
            or (best_cp - second_cp) > TRIVIAL_GAP_CP
        )
        evals.append({
            "color": "white" if mover_is_white else "black",
            "matched": best_move == move,
            "loss": loss,
            "trivial": trivial,
        })
    return evals or None


def summarize_evals(
    evals: list[dict], *,
    white_seconds: int = 0, black_seconds: int = 0,
    white_move_total: int = 0, black_move_total: int = 0,
) -> dict:
    """Fold per-ply grades into the per-player analysis dict that gets
    stored, displayed, and flag-checked. Move times use the full-game clock
    totals (not just analyzed plies) since that's what the clock tracked."""
    analysis: dict = {"version": ANALYSIS_VERSION, "depth": ANALYSIS_DEPTH}
    for color in ("white", "black"):
        rows = [e for e in evals if e["color"] == color]
        if not rows:
            analysis[color] = {"moves": 0}
            continue
        acpl = sum(r["loss"] for r in rows) / len(rows)
        nontrivial = [r for r in rows if not r["trivial"]]
        matched = sum(1 for r in nontrivial if r["matched"])
        match_pct = round(100.0 * matched / len(nontrivial), 1) if nontrivial else None
        seconds = white_seconds if color == "white" else black_seconds
        move_total = white_move_total if color == "white" else black_move_total
        avg_seconds = round(seconds / move_total, 1) if seconds and move_total else None
        analysis[color] = {
            "moves": len(rows),
            "nontrivial": len(nontrivial),
            "acpl": round(acpl, 1),
            "match_pct": match_pct,
            "est_elo": estimate_elo_from_acpl(acpl),
            "avg_seconds": avg_seconds,
        }
    return analysis


def flag_suspect(
    analysis: dict, *,
    white_id: int, black_id: int, winner_id: int | None,
    bot_user_id: int | None, bot_elo: int | None,
) -> int | None:
    """Return the human uid to cheat-flag, or None.

    Only bot games at/above FLAG_MIN_BOT_ELO where the human won or drew
    are eligible; the human's side must show sustained near-engine accuracy
    over enough non-trivial moves.
    """
    if bot_user_id is None or bot_elo is None or bot_elo < FLAG_MIN_BOT_ELO:
        return None
    if winner_id == bot_user_id:
        return None
    human_id = white_id if black_id == bot_user_id else black_id
    side = analysis.get("white" if human_id == white_id else "black") or {}
    if (side.get("nontrivial") or 0) < FLAG_MIN_NONTRIVIAL:
        return None
    acpl = side.get("acpl")
    match_pct = side.get("match_pct")
    if acpl is None or match_pct is None:
        return None
    if acpl <= FLAG_MAX_ACPL and match_pct >= FLAG_MIN_MATCH_PCT:
        return human_id
    return None


def format_analysis_lines(analysis: dict | None, white_name: str, black_name: str) -> str:
    """The public analysis block for the game-over embed and !chess view.
    Empty string when there's nothing displayable."""
    if not analysis:
        return ""
    lines: list[str] = []
    for key, name, glyph in (("white", white_name, "♙"), ("black", black_name, "♟")):
        side = analysis.get(key)
        if not side or not side.get("moves"):
            continue
        parts = [f"est. **~{side['est_elo']:,} Elo**", f"{side['acpl']:g} ACPL"]
        if side.get("match_pct") is not None:
            parts.append(f"{side['match_pct']:g}% top moves")
        if side.get("avg_seconds"):
            parts.append(f"{side['avg_seconds']:g}s/move")
        lines.append(f"{glyph} **{name}** — " + " · ".join(parts))
    if not lines:
        return ""
    footer = f"*Estimated from move accuracy (Stockfish depth {analysis.get('depth', ANALYSIS_DEPTH)}) — rough guide only.*"
    return "**📊 Engine Analysis**\n" + "\n".join(lines) + f"\n{footer}"


async def _engine_evaluate(engine: chess.engine.UciProtocol, board: chess.Board):
    """One graded engine query: (best_move, best_cp, second_cp), cp clamped
    and from the side-to-move's POV."""
    infos = await engine.analyse(
        board,
        chess.engine.Limit(depth=ANALYSIS_DEPTH, time=ANALYSIS_TIME_PER_POSITION),
        multipv=ANALYSIS_MULTIPV,
    )
    if not infos:
        return None, None, None

    def _cp(info) -> int | None:
        score = info.get("score")
        if score is None:
            return None
        raw = score.relative.score(mate_score=MATE_SCORE_CP)
        return max(-CP_CAP, min(CP_CAP, raw))

    best = infos[0]
    pv = best.get("pv") or []
    best_move = pv[0] if pv else None
    second_cp = _cp(infos[1]) if len(infos) > 1 else None
    return best_move, _cp(best), second_cp


async def _run_engine_analysis(
    pgn: str, white_seconds: int, black_seconds: int,
) -> dict | None:
    """Engine-facing seam: spawn one Stockfish, grade the game, summarize.
    Returns None when the game is too short, unparseable, or the engine is
    unavailable. Tests monkeypatch this with canned analysis dicts."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        return None
    moves = list(game.mainline_moves())
    if len(moves) < MIN_PLIES_FOR_ANALYSIS:
        return None
    try:
        transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    except (chess.engine.EngineError, FileNotFoundError, OSError) as e:
        logging.warning(f"chess analysis: engine unavailable: {e}")
        return None
    try:
        async def evaluate(board: chess.Board):
            return await _engine_evaluate(engine, board)

        evals = await _collect_move_evals(pgn, evaluate)
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()
    if not evals:
        return None
    return summarize_evals(
        evals,
        white_seconds=white_seconds, black_seconds=black_seconds,
        white_move_total=(len(moves) + 1) // 2, black_move_total=len(moves) // 2,
    )


async def _post_cheat_alert(
    bot, guild, channel, *, report_id: int, suspect_id: int, suspect_name: str,
    side: dict, bot_elo: int, result_word: str,
) -> None:
    """Best-effort alert to the global admin-log channel. Swallows errors —
    a broken alert must never disturb the game channel."""
    chan_id = state.bot_settings.get("admin_log_channel")
    if not chan_id or bot is None:
        return
    try:
        log_channel = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        return
    try:
        flagged_total = await count_flagged_reports(suspect_id)
    except Exception:
        flagged_total = None
    guild_name = guild.name if guild is not None else "DM"
    channel_ref = channel.mention if hasattr(channel, "mention") else str(getattr(channel, "id", "?"))
    stat_line = (
        f"~{side.get('est_elo', 0):,} Elo est. — {side.get('acpl', 0):g} ACPL, "
        f"{side.get('match_pct', 0):g}% top moves over {side.get('nontrivial', 0)} non-trivial"
    )
    lines = [
        f"**User:** {suspect_name} (`{suspect_id}`)",
        f"**Game:** #{report_id} — {result_word} vs a **{bot_elo} Elo** bot",
        f"**Play strength:** {stat_line}",
    ]
    if side.get("avg_seconds"):
        lines.append(f"**Avg move time:** {side['avg_seconds']:g}s")
    if flagged_total is not None:
        lines.append(f"**Flagged games (all-time):** {flagged_total}")
    lines.append(f"**Where:** {guild_name} · {channel_ref}")
    lines.append(f"Review with `!chess view {report_id}`.")
    try:
        await log_channel.send(embed=emb(
            "🚨 Possible Chess Cheating",
            "\n".join(lines) + "\n\n*Heuristic flag — review before acting.*",
            C_RED,
        ))
    except (discord.Forbidden, discord.HTTPException):
        pass


async def analyze_and_post(
    *, bot, channel, guild, report_id: int, pgn: str,
    white_id: int, black_id: int, winner_id: int | None, elo: int | None,
    white_name: str, black_name: str,
    white_seconds: int = 0, black_seconds: int = 0,
    embed_msg_id: int | None = None,
) -> None:
    """Background task per finished game: analyze, persist, display, alert.
    Every step is best-effort — analysis must never disturb the game flow."""
    try:
        analysis = await _run_engine_analysis(pgn, white_seconds, black_seconds)
    except Exception as e:
        logging.error(f"chess analysis failed for report {report_id}: {e}", exc_info=True)
        return
    if analysis is None:
        return

    bot_user_id = bot.user.id if bot is not None and bot.user is not None else None
    suspect_id = None
    if elo is not None:
        suspect_id = flag_suspect(
            analysis, white_id=white_id, black_id=black_id,
            winner_id=winner_id, bot_user_id=bot_user_id, bot_elo=int(elo),
        )
    try:
        await save_chess_analysis(report_id, analysis, suspect_id)
    except Exception as e:
        logging.error(f"chess analysis save failed for report {report_id}: {e}", exc_info=True)

    # Show the stats where the game just ended: edit the game-over embed in
    # place, or fall back to a small follow-up embed if it's gone.
    block = format_analysis_lines(analysis, white_name, black_name)
    if block:
        edited = False
        if embed_msg_id is not None:
            try:
                msg = await channel.fetch_message(int(embed_msg_id))
                if msg.embeds:
                    e = msg.embeds[0]
                    new_desc = (e.description or "") + "\n\n" + block
                    if len(new_desc) <= 4000:  # embed description hard cap is 4096
                        e.description = new_desc
                        await msg.edit(embed=e)
                        edited = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        if not edited:
            try:
                await channel.send(embed=emb(
                    "📊 Chess Analysis", f"Game `#{report_id}`\n\n{block}", C_GREY,
                ), silent=True)
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

    if suspect_id is not None:
        suspect_name = white_name if suspect_id == white_id else black_name
        side = analysis.get("white" if suspect_id == white_id else "black") or {}
        await _post_cheat_alert(
            bot, guild, channel,
            report_id=report_id, suspect_id=suspect_id, suspect_name=suspect_name,
            side=side, bot_elo=int(elo),
            result_word="won" if winner_id == suspect_id else "drew",
        )
