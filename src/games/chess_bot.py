from __future__ import annotations

import os
import random
import shutil

import chess
import chess.engine


# Three-tier strength model:
#   - Native UCI_Elo (1320-3190): Stockfish's built-in strength limiter.
#   - MultiPV pick-worse (300-1319): ask Stockfish for top-10 moves and pick
#     the Nth-best where N scales with Elo. Plays bad chess but reacts to
#     direct threats (won't hang the queen or walk into mate-in-1 if at least
#     one of the top-10 doesn't).
#   - Random blend (100-299): true beginner — at Elo 100 nearly all moves are
#     random; ramps to 0% random at the MultiPV floor.
STOCKFISH_NATIVE_ELO_MIN = 1320
STOCKFISH_NATIVE_ELO_MAX = 3190
MULTIPV_FLOOR = 300
MULTIPV_COUNT = 10

# Analysis depth in the MultiPV tier is INVERTED with respect to Elo:
#   - Low Elo (300):  high depth (4 plies) + worst rank (10th of top-10).
#     Stockfish actually sees one-move-deep threats so it doesn't blunder
#     captures/mates, but it picks the worst defensive option from that
#     position-aware analysis. Plays bad chess, not blind chess.
#   - High Elo (1319): low depth (2 plies) + best rank (1st). Stockfish at
#     depth 2 is genuinely weak (~1500 effective Elo, no 3-ply tactics) —
#     picking its best move at that depth produces a coherent but limited
#     player just below the native UCI_Elo floor.
# This inversion fixes the prior problem where Elo 1100-1319 felt stronger
# than 1400-1600 native: previously the upper MultiPV range used deep search
# at rank 1, which gave full-strength play. Now depth caps strength even at
# rank 1, so the curve is monotonic across the MultiPV→native boundary.
MULTIPV_DEPTH_LOW_ELO = 4   # at MULTIPV_FLOOR
MULTIPV_DEPTH_HIGH_ELO = 2  # at STOCKFISH_NATIVE_ELO_MIN - 1

# What we accept from users via !chess @Bot <elo>.
ELO_MIN = 100
ELO_MAX = STOCKFISH_NATIVE_ELO_MAX
ELO_DEFAULT = 1320

# Per-move think time at native Elo. Sub-native paths use a short analyse
# time instead since they only need the move list, not deep search.
MOVE_TIME_SECONDS = 0.5
ANALYSE_TIME_SECONDS = 0.3

# Debian's `stockfish` apt package installs at /usr/games/stockfish, which is
# NOT on the default PATH for non-login shells (which is what asyncio's
# subprocess sees). We can't rely on PATH lookup alone — must fall back to
# the known install location.
_DEBIAN_STOCKFISH_PATH = "/usr/games/stockfish"


def _resolve_stockfish_path() -> str:
    """Resolve the stockfish binary path. STOCKFISH_PATH env var wins, then
    PATH lookup (for dev machines), then Debian's apt install location.
    Resolved per-call so test monkeypatches of the env var take effect."""
    explicit = os.environ.get("STOCKFISH_PATH")
    if explicit:
        return explicit
    found = shutil.which("stockfish")
    if found:
        return found
    return _DEBIAN_STOCKFISH_PATH


def clamp_elo(elo: int) -> int:
    if elo < ELO_MIN:
        return ELO_MIN
    if elo > ELO_MAX:
        return ELO_MAX
    return elo


def random_move_probability(elo: int) -> float:
    """For the random-blend tier (100..MULTIPV_FLOOR-1): linear, 1.0 at
    ELO_MIN, 0.0 at MULTIPV_FLOOR. Returns 0.0 outside this range."""
    if elo >= MULTIPV_FLOOR:
        return 0.0
    if elo <= ELO_MIN:
        return 1.0
    span = MULTIPV_FLOOR - ELO_MIN  # 200
    return (MULTIPV_FLOOR - elo) / span


def multipv_rank_for_elo(elo: int) -> int:
    """For the MultiPV tier (MULTIPV_FLOOR..STOCKFISH_NATIVE_ELO_MIN-1):
    which 1-indexed rank to pick from the top-N analysis. Elo MULTIPV_FLOOR
    picks rank MULTIPV_COUNT (worst), Elo STOCKFISH_NATIVE_ELO_MIN-1 picks
    rank 1 (best). Linear interpolation between."""
    if elo <= MULTIPV_FLOOR:
        return MULTIPV_COUNT
    if elo >= STOCKFISH_NATIVE_ELO_MIN:
        return 1
    span = STOCKFISH_NATIVE_ELO_MIN - 1 - MULTIPV_FLOOR  # 1019
    progress = (elo - MULTIPV_FLOOR) / span  # 0..1 mapping low→high Elo
    # Want progress 0 → rank MULTIPV_COUNT, progress 1 → rank 1.
    rank = round(MULTIPV_COUNT - progress * (MULTIPV_COUNT - 1))
    return max(1, min(MULTIPV_COUNT, rank))


def multipv_depth_for_elo(elo: int) -> int:
    """Analysis depth for the MultiPV tier. INVERTED — high depth at low Elo
    (so the bot sees threats and doesn't blunder captures) ramping down to
    low depth at high Elo (so even rank-1 is a shallow-search best move,
    keeping the upper MultiPV range comparable to native UCI_Elo 1320)."""
    if elo <= MULTIPV_FLOOR:
        return MULTIPV_DEPTH_LOW_ELO
    if elo >= STOCKFISH_NATIVE_ELO_MIN:
        return MULTIPV_DEPTH_HIGH_ELO
    span_elo = STOCKFISH_NATIVE_ELO_MIN - 1 - MULTIPV_FLOOR
    span_depth = MULTIPV_DEPTH_LOW_ELO - MULTIPV_DEPTH_HIGH_ELO
    progress = (elo - MULTIPV_FLOOR) / span_elo  # 0 at low, 1 at high
    # progress 0 → MULTIPV_DEPTH_LOW_ELO; progress 1 → MULTIPV_DEPTH_HIGH_ELO
    depth = round(MULTIPV_DEPTH_LOW_ELO - progress * span_depth)
    lo, hi = sorted((MULTIPV_DEPTH_LOW_ELO, MULTIPV_DEPTH_HIGH_ELO))
    return max(lo, min(hi, depth))


def _pick_random_legal(board: chess.Board) -> chess.Move | None:
    legal = list(board.legal_moves)
    return random.choice(legal) if legal else None


async def _native_elo_move(engine: chess.engine.UciProtocol, board: chess.Board,
                           elo: int) -> chess.Move:
    """Stockfish's built-in strength limiter — only works for 1320-3190."""
    await engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
    result = await engine.play(board, chess.engine.Limit(time=MOVE_TIME_SECONDS))
    if result.move is None:
        raise chess.engine.EngineError("Stockfish returned no move")
    return result.move


async def _multipv_worse_move(engine: chess.engine.UciProtocol, board: chess.Board,
                              elo: int) -> chess.Move:
    """Ask for top-MULTIPV_COUNT moves at depth-limited analysis, pick the Nth
    where N depends on Elo. Both the depth and the rank scale with Elo: low
    Elo gets shallow analysis (weak top-10) AND worse rank within that top-10."""
    depth = multipv_depth_for_elo(elo)
    infos = await engine.analyse(
        board,
        chess.engine.Limit(depth=depth),
        multipv=MULTIPV_COUNT,
    )
    # Filter to entries with a usable principal-variation. Stockfish always
    # returns at least one but may return fewer than MULTIPV_COUNT in positions
    # with few legal moves.
    pvs = [info for info in infos if info.get("pv")]
    if not pvs:
        # Fall back to a single-move play to avoid stalling the game.
        result = await engine.play(board, chess.engine.Limit(time=ANALYSE_TIME_SECONDS))
        if result.move is None:
            raise chess.engine.EngineError("Stockfish returned no move")
        return result.move
    target_rank = multipv_rank_for_elo(elo)  # 1-indexed
    # Clamp the rank to what's actually available in this position.
    idx = min(target_rank, len(pvs)) - 1
    return pvs[idx]["pv"][0]


async def _blended_move(engine: chess.engine.UciProtocol, board: chess.Board,
                        elo: int) -> chess.Move:
    """True-beginner tier: get the MultiPV-worst move then with high
    probability replace it with a fully random legal move."""
    engine_move = await _multipv_worse_move(engine, board, elo)
    p_random = random_move_probability(elo)
    if p_random > 0 and random.random() < p_random:
        random_move = _pick_random_legal(board)
        if random_move is not None:
            return random_move
    return engine_move


async def pick_move(fen: str, elo: int) -> chess.Move:
    """Spawn Stockfish, get one move at the target Elo, close. Per-move spawn
    keeps zero processes alive when nobody's playing chess."""
    board = chess.Board(fen=fen)
    elo = clamp_elo(elo)

    transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    try:
        if elo >= STOCKFISH_NATIVE_ELO_MIN:
            return await _native_elo_move(engine, board, elo)
        if elo >= MULTIPV_FLOOR:
            return await _multipv_worse_move(engine, board, elo)
        return await _blended_move(engine, board, elo)
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()
