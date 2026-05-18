from __future__ import annotations

import logging
import os
import random
import shutil
from pathlib import Path

import chess
import chess.engine


# Three-tier strength model:
#   - Sub-Maia (100-1000): Maia 1100 as the baseline (real human-shaped moves),
#     with two probabilistic degraders layered on top:
#       * random-move blend: swap Maia's pick for a random legal move
#       * extra-blunder injection: swap for a SEE-losing move (drops material)
#     Curves scale with Elo so 100 plays mostly random, 1000 plays nearly
#     pure Maia 1100 with only ~1 extra blunder per game.
#   - Maia (1100-1900): human-trained neural networks, one per 100-Elo bin.
#     A single forward pass per move (`go nodes 1`) returns the move a real
#     player at that rating would make. Each weights file is bundled in
#     ./maia_weights/ and consumed by lc0 (Leela Chess Zero) as a UCI engine.
#   - Native (2000+): Stockfish's UCI_LimitStrength + UCI_Elo limiter.
#     Stockfish's native floor is 1320 but at that level it plays nothing
#     like a real 1320 human; we let Maia cover everything up to 1900 and
#     only switch back to Stockfish well above Maia's training range.
STOCKFISH_NATIVE_ELO_MIN = 2000
STOCKFISH_NATIVE_ELO_MAX = 3190
MAIA_ELO_MIN = 1100
MAIA_ELO_MAX = 1900
# Baseline Maia network used for ALL sub-Maia Elos (100-1000). The lower
# Elos are produced by degrading this baseline, not by training models at
# those ratings (Maia has no networks below 1100).
SUB_MAIA_BASELINE_ELO = 1100

# What we accept from users via !chess @Bot <elo>. Caller (the cog) rounds
# any non-multiple-of-100 input at the command boundary, so internally we
# can assume Elo is always a multiple of 100.
ELO_MIN = 100
ELO_MAX = STOCKFISH_NATIVE_ELO_MAX
ELO_DEFAULT = 1300

# Per-move think time at native Stockfish Elo. The sub-Maia and Maia paths
# both use single-node policy-net calls (no time budget needed).
MOVE_TIME_SECONDS = 0.5

# Debian's `stockfish` apt package installs at /usr/games/stockfish, which is
# NOT on the default PATH for non-login shells (which is what asyncio's
# subprocess sees). We can't rely on PATH lookup alone — must fall back to
# the known install location.
_DEBIAN_STOCKFISH_PATH = "/usr/games/stockfish"

# Maia weights live alongside the source tree, copied into the Docker image
# at /app/maia_weights/. One .pb.gz file per 100-Elo bin from 1100 to 1900.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAIA_WEIGHTS_DIR = _REPO_ROOT / "maia_weights"


# Sub-Maia pool size: how many of Maia 1100's top-N policy moves we sample
# uniformly from. Each move in Maia's top-N is a real move that 1100-rated
# humans actually play in this position — just less common ones at the tail.
# Sampling from a wider pool models a weaker player making more "unlikely but
# human" choices. Even at the top of the range (Elo 1000) the pool is small
# but >1 so sampling still varies the bot's response within Maia's most
# confident moves. MULTIPV_COUNT below is the engine query size and must
# be ≥ the largest pool anchor.
MULTIPV_COUNT = 11
_MAIA_POOL_SIZE_ANCHORS: list[tuple[int, int]] = [
    (100, 11),
    (400, 8),
    (700, 5),
    (800, 4),
    (900, 3),
    (1000, 2),
]

# Probability of injecting an EXTRA blunder on top of Maia's sampled move.
# Swaps for a SEE-losing alternative (a move that drops material per static
# exchange evaluation). Only fires at low Elo (≤500) — above 500, the wider
# Maia pool already includes Maia's natural mistake distribution, no need
# to force additional ones.
_EXTRA_BLUNDER_ANCHORS: list[tuple[int, float]] = [
    (100, 0.10),
    (400, 0.01),
    (500, 0.0),
    (1000, 0.0),
]

# Standard chess piece values used for the SEE swap loop. King is "infinite"
# (sentinel — any side losing the king has already lost the game).
_PIECE_VALUES: dict[chess.PieceType, int] = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 10_000,
}


def round_elo_to_bin(elo: int) -> int:
    """Round an Elo value to the nearest multiple of 100. Maia weights are
    indexed by bin (1100, 1200, ..., 1900) so the cog rounds inputs at the
    command boundary; pick_move also rounds defensively for callers that
    bypass the cog (e.g. tests)."""
    return int(round(elo / 100.0)) * 100


def maia_weights_path(elo: int) -> Path:
    """Filesystem path to the Maia weights file for the given Elo bin."""
    return _MAIA_WEIGHTS_DIR / f"maia-{elo}.pb.gz"


def _resolve_lc0_path() -> str | None:
    """Find the lc0 binary. PATH first, then common Linux install locations.
    Returns None if not found — callers fall back to Stockfish."""
    found = shutil.which("lc0")
    if found:
        return found
    for candidate in ("/usr/games/lc0", "/usr/local/bin/lc0"):
        if os.path.exists(candidate):
            return candidate
    return None


def _interp_int(anchors: list[tuple[int, int]], x: int) -> int:
    """Piecewise-linear interpolation with integer output. Clamps below the
    first and above the last anchor."""
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            progress = (x - x0) / (x1 - x0)
            return round(y0 + progress * (y1 - y0))
    return anchors[-1][1]


def _interp_float(anchors: list[tuple[int, float]], x: int) -> float:
    """Piecewise-linear interpolation over (x, y) anchors with float output.
    Clamps below the first and above the last anchor."""
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            progress = (x - x0) / (x1 - x0)
            return y0 + progress * (y1 - y0)
    return anchors[-1][1]  # unreachable


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


def maia_pool_size_for_elo(elo: int) -> int:
    """How many of Maia 1100's top-N policy moves to sample uniformly from.
    Larger pool = more variety (and more of Maia's less-likely human moves,
    which models weaker play). Pool=2 at Elo 1000 keeps a little response
    variability even at the top of the sub-Maia range; pool=11 at Elo 100
    samples broadly from Maia 1100's tail."""
    return _interp_int(_MAIA_POOL_SIZE_ANCHORS, elo)


def extra_blunder_probability_for_elo(elo: int) -> float:
    """Probability of swapping Maia's pick for a SEE-losing alternative
    (a move that drops material). Only fires at sub-500 Elo — above that,
    the wider Maia sampling pool already produces enough natural mistakes."""
    return _interp_float(_EXTRA_BLUNDER_ANCHORS, elo)


def _piece_value(piece: chess.Piece | None) -> int:
    if piece is None:
        return 0
    return _PIECE_VALUES[piece.piece_type]


def see_capture(board: chess.Board, square: chess.Square, side_to_move: chess.Color) -> int:
    """Static Exchange Evaluation: net material delta on `square` from the
    perspective of `side_to_move` if they initiate the capture and both sides
    always recapture with the cheapest available attacker.

    Returns a positive value if `side_to_move` wins material by capturing,
    negative if they lose material, 0 for an even trade.

    Algorithm: classic cheapest-attacker swap-list (chessprogramming wiki).
    Build a gain[] array where gain[d] is the net material to the initiator
    after d captures, then minimax it backwards.

    Known limitation: pinned defenders are still counted as recapturers.
    Acceptable for sub-1000 Elo play.
    """
    target = board.piece_at(square)
    if target is None:
        return 0

    initiator_attackers = board.attackers(side_to_move, square)
    if not initiator_attackers:
        return 0

    work = board.copy(stack=False)
    gains: list[int] = [_piece_value(target)]
    color = side_to_move

    cheapest_sq = min(initiator_attackers, key=lambda sq: _piece_value(work.piece_at(sq)))
    last_attacker_value = _piece_value(work.piece_at(cheapest_sq))
    work.remove_piece_at(cheapest_sq)
    color = not color

    while True:
        attackers = work.attackers(color, square)
        if not attackers:
            break
        gains.append(last_attacker_value - gains[-1])
        cheapest_sq = min(attackers, key=lambda sq: _piece_value(work.piece_at(sq)))
        last_attacker_value = _piece_value(work.piece_at(cheapest_sq))
        work.remove_piece_at(cheapest_sq)
        color = not color

    for i in range(len(gains) - 1, 0, -1):
        gains[i - 1] = -max(-gains[i - 1], gains[i])
    return gains[0]


def _find_see_losing_move(board: chess.Board, exclude: chess.Move | None = None) -> chess.Move | None:
    """Walk legal moves and return a uniformly-random one whose destination
    square has SEE < 0 for the moving side after the move is played — i.e.
    a move that drops material in a static exchange.

    Used by the extra-blunder injector to find a concrete "losing piece"
    move when we want to degrade Maia's pick. Excludes `exclude` (typically
    Maia's chosen move) so the injector actually changes the move.

    Returns None if no SEE-losing move exists in the position (rare in
    middlegames; common in simplified endgames). Caller falls back to
    keeping Maia's original move."""
    mover = board.turn
    opp = not mover
    losing: list[chess.Move] = []
    for mv in board.legal_moves:
        if exclude is not None and mv == exclude:
            continue
        # SEE from the opponent's perspective: if they would gain material by
        # capturing on our move's destination square AFTER we move there,
        # that move drops material.
        after = board.copy(stack=False)
        after.push(mv)
        if see_capture(after, mv.to_square, opp) > 0:
            losing.append(mv)
    if not losing:
        return None
    return random.choice(losing)


async def _native_elo_move(engine: chess.engine.UciProtocol, board: chess.Board,
                           elo: int) -> chess.Move:
    """Stockfish's built-in strength limiter — only works for 1320-3190."""
    await engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
    result = await engine.play(board, chess.engine.Limit(time=MOVE_TIME_SECONDS))
    if result.move is None:
        raise chess.engine.EngineError("Stockfish returned no move")
    return result.move


async def _spawn_maia_engine(elo: int):
    """Spawn lc0 with the Maia weights file for the given Elo bin.

    Raises chess.engine.EngineError if lc0 isn't installed or the weights
    file is missing — caller catches and falls back."""
    lc0_path = _resolve_lc0_path()
    if lc0_path is None:
        raise chess.engine.EngineError(
            "lc0 binary not found; install lc0 or check PATH"
        )
    weights = maia_weights_path(elo)
    if not weights.exists():
        raise chess.engine.EngineError(f"Maia weights file missing: {weights}")
    return await chess.engine.popen_uci([
        lc0_path,
        f"--weights={weights}",
        "--backend=eigen",  # CPU backend; no GPU required
    ])


async def _maia_move(board: chess.Board, elo: int) -> chess.Move:
    """Spawn Maia at the given Elo bin and ask for one move (`go nodes 1`).
    Used by the pure-Maia tier (Elo 1100-1900); sub-Maia uses _maia_top_n."""
    transport, engine = await _spawn_maia_engine(elo)
    try:
        result = await engine.play(board, chess.engine.Limit(nodes=1))
        if result.move is None:
            raise chess.engine.EngineError("Maia returned no move")
        return result.move
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()


async def _maia_top_n(board: chess.Board, elo: int, n: int) -> list[chess.Move]:
    """Spawn Maia and return its top-N policy moves (ranked by the policy
    net's probabilities, most-likely-human first). When the position has
    fewer than N legal moves, returns whatever was available."""
    transport, engine = await _spawn_maia_engine(elo)
    try:
        infos = await engine.analyse(
            board,
            chess.engine.Limit(nodes=1),
            multipv=max(1, n),
        )
        moves = [info["pv"][0] for info in infos if info.get("pv")]
        if not moves:
            raise chess.engine.EngineError("Maia returned no moves")
        return moves
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()


async def _sub_maia_move(board: chess.Board, elo: int) -> chess.Move:
    """Elo 100-1000 tier: sample uniformly from Maia 1100's top-N policy
    moves, then optionally inject a forced blunder.

    1. Ask Maia 1100 for its top-pool_size moves (each one is a real move
       1100-rated humans actually play, just less common at the tail).
    2. Sample one uniformly from the pool.
    3. With extra_blunder_probability_for_elo (only nonzero below Elo 500):
       swap the chosen move for a SEE-losing alternative if one exists.
    4. Return.

    No pure-random replacement — at any Elo, every move comes from either
    Maia's policy distribution or an explicit SEE-losing move. Both are
    moves a real beginner might play; neither is engine-flavored noise.
    """
    pool_size = maia_pool_size_for_elo(elo)
    top_n = await _maia_top_n(board, SUB_MAIA_BASELINE_ELO, pool_size)
    # Clamp in case Maia returned fewer than pool_size in tight positions.
    candidates = top_n[:pool_size]
    move = random.choice(candidates) if len(candidates) > 1 else candidates[0]

    if random.random() < extra_blunder_probability_for_elo(elo):
        blunder = _find_see_losing_move(board, exclude=move)
        if blunder is not None:
            move = blunder

    return move


async def pick_move(fen: str, elo: int) -> chess.Move:
    """Dispatch to the right chess engine based on Elo. Spawn-per-move keeps
    zero processes alive when nobody's playing chess.

    Routing (assuming Elo is a multiple of 100 — caller rounds at the
    command boundary, and pick_move rounds defensively):
      - Elo 100-1000: Maia 1100 + random-blend + extra-blunder degraders.
      - Elo 1100-1900: pure Maia at the matching weights bin.
      - Elo 2000+: Stockfish with UCI_LimitStrength + UCI_Elo.

    If Maia fails to spawn (lc0 missing, weights missing) anywhere in the
    100-1900 range, fall back to Stockfish at the nearest supported Elo.
    """
    board = chess.Board(fen=fen)
    elo = clamp_elo(round_elo_to_bin(elo))

    # Sub-Maia tier: Maia 1100 + degraders.
    if elo < MAIA_ELO_MIN:
        try:
            return await _sub_maia_move(board, elo)
        except (chess.engine.EngineError, FileNotFoundError, OSError) as e:
            logging.warning(
                f"Maia unavailable for sub-Maia Elo {elo}: {e}; "
                "falling back to Stockfish UCI_Elo 1320"
            )
            return await _stockfish_fallback(board, 1320)

    # Maia tier: single-node policy-net call at the matching bin.
    if elo <= MAIA_ELO_MAX:
        try:
            return await _maia_move(board, elo)
        except (chess.engine.EngineError, FileNotFoundError, OSError) as e:
            logging.warning(
                f"Maia engine unavailable at Elo {elo}: {e}; "
                "falling back to Stockfish UCI_Elo 1320"
            )
            return await _stockfish_fallback(board, 1320)

    # Native Stockfish tier.
    return await _stockfish_fallback(board, elo)


async def _stockfish_fallback(board: chess.Board, elo: int) -> chess.Move:
    """Run Stockfish's UCI_Elo limiter at the given strength. Used both as
    the 2000+ native tier and as the fallback when Maia is unavailable."""
    transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    try:
        # Stockfish's native floor is 1320; clamp up if asked for less.
        effective = max(elo, 1320)
        return await _native_elo_move(engine, board, effective)
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()
