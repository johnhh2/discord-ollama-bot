from __future__ import annotations

import os
import random
import shutil

import chess
import chess.engine


# Two-tier strength model:
#   - Native UCI_Elo (1320-3190): Stockfish's built-in strength limiter.
#   - Sub-native MultiPV+SEE (100-1319): ask Stockfish for top-10 moves at an
#     Elo-scaled depth, sample uniformly from the top-N (N also Elo-scaled),
#     and probabilistically filter out moves that would hang material via a
#     post-move Static Exchange Evaluation check. See the per-Elo curves below
#     for how depth, pool size, and filter probability scale together.
STOCKFISH_NATIVE_ELO_MIN = 1320
STOCKFISH_NATIVE_ELO_MAX = 3190
MULTIPV_COUNT = 10

# What we accept from users via !chess @Bot <elo>.
ELO_MIN = 100
ELO_MAX = STOCKFISH_NATIVE_ELO_MAX
ELO_DEFAULT = 1320

# Per-move think time at native Elo. Sub-native paths use depth-limited
# analyse instead since they need the multipv list, not deep iterative play.
MOVE_TIME_SECONDS = 0.5

# Debian's `stockfish` apt package installs at /usr/games/stockfish, which is
# NOT on the default PATH for non-login shells (which is what asyncio's
# subprocess sees). We can't rely on PATH lookup alone — must fall back to
# the known install location.
_DEBIAN_STOCKFISH_PATH = "/usr/games/stockfish"


# Piecewise-linear curve anchors. Each list is [(elo, value), ...] sorted by
# elo. _interp() handles linear interpolation between adjacent anchors and
# clamps to the endpoints outside the range.
_DEPTH_ANCHORS: list[tuple[int, int]] = [
    (100, 1),     # Immediate captures only (depth-1 + quiescence)
    (300, 2),     # Hanging pieces, mate-in-1
    (500, 3),     # Simple 1-move tactics
    (700, 5),     # 2-move combinations, basic forks
    (900, 7),     # Most 2-3 move tactics
    (1000, 8),    # 3-move tactics, simple mating attacks
    (1100, 10),   # 4-move tactics
    (1200, 12),   # 5-move forced sequences
    (1319, 14),   # Strong club-player tactics — boundary with native tier
]

_POOL_SIZE_ANCHORS: list[tuple[int, int]] = [
    (100, 10),
    (400, 8),
    (600, 6),
    (800, 4),
    (1000, 3),
    (1100, 2),
    (1200, 1),
    (1319, 1),
]

# Filter probability calibrated so the upper-mid range plays competently —
# ≈ 1 hang per 40-move game at Elo 1000+. At the bottom the filter is
# permissive (0.20 at Elo 100) so beginner-feeling blunders come through.
# Above 1000 the strength gradient comes from deeper search + shrinking pool,
# so we cap P(filter) at 0.975.
_FILTER_PROB_ANCHORS: list[tuple[int, float]] = [
    (100, 0.20),
    (400, 0.50),
    (700, 0.80),
    (1000, 0.975),
    (1319, 0.975),
]

# Probability of replacing Stockfish's suggestion with a fully random legal
# move. Real beginners play moves that aren't even in Stockfish's top-10 —
# odd rook lifts, edge-pawn shuffles, etc. Decays to 0 by Elo 400 so the
# mid-range relies purely on the MultiPV+filter system.
_RANDOM_BLEND_ANCHORS: list[tuple[int, float]] = [
    (100, 0.80),
    (200, 0.50),
    (300, 0.20),
    (400, 0.0),
    (1319, 0.0),
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


def _interp_int(anchors: list[tuple[int, int]], x: int) -> int:
    """Piecewise-linear interpolation over (x, y) anchors with integer output.
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
            return round(y0 + progress * (y1 - y0))
    return anchors[-1][1]  # unreachable


def _interp_float(anchors: list[tuple[int, float]], x: int) -> float:
    """Piecewise-linear interpolation over (x, y) anchors with float output."""
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


def multipv_depth_for_elo(elo: int) -> int:
    """Stockfish analysis depth for the sub-native MultiPV tier. Ramps UP with
    Elo (opposite of the previous curve) so deeper Elo → meaningfully ordered
    top-10 list. See _DEPTH_ANCHORS for the full ladder."""
    return _interp_int(_DEPTH_ANCHORS, elo)


def multipv_pool_size_for_elo(elo: int) -> int:
    """How many of the top-N PVs to sample from uniformly. Shrinks with Elo:
    Elo 100 samples top-10 (noisy), Elo 1000 samples top-3 (focused), Elo
    1200+ samples only rank-1 (deterministic, rejoins native tier cleanly)."""
    return _interp_int(_POOL_SIZE_ANCHORS, elo)


def safety_filter_probability_for_elo(elo: int) -> float:
    """Probability that the SEE-based 'don't hang a piece' filter fires for a
    given move. Permissive at the bottom (0.20 at Elo 100) so beginner play
    can blunder; tight by Elo 1000+ (0.975, capped) so competent play rarely
    hangs material. Above 1000 the strength curve is driven by depth/pool."""
    return _interp_float(_FILTER_PROB_ANCHORS, elo)


def random_blend_probability_for_elo(elo: int) -> float:
    """Probability of swapping Stockfish's pick for a fully random legal move
    — models true-beginner play where moves aren't even in Stockfish's top-10.
    0.80 at Elo 100, decays to 0 by Elo 400 and stays 0 above that."""
    return _interp_float(_RANDOM_BLEND_ANCHORS, elo)


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
    after d captures, then minimax it backwards — at each ply, the side to
    move chooses between continuing the exchange or standing pat.

    We mutate a working copy of the board, removing each attacker as it
    captures and re-querying attackers() each ply, which transparently picks
    up x-ray attackers behind whatever piece just moved.

    Known limitation: pinned defenders are still counted as recapturers.
    Acceptable for sub-1000 Elo play; documented in module-level comments.
    """
    target = board.piece_at(square)
    if target is None:
        return 0

    # The initiator MUST have an attacker on the square — otherwise there's
    # no capture to evaluate.
    initiator_attackers = board.attackers(side_to_move, square)
    if not initiator_attackers:
        return 0

    work = board.copy(stack=False)

    # gain[0] = value the initiator nets after capturing the target.
    gains: list[int] = [_piece_value(target)]
    color = side_to_move

    # First capture: remove the cheapest initiator-attacker.
    cheapest_sq = min(initiator_attackers, key=lambda sq: _piece_value(work.piece_at(sq)))
    last_attacker_value = _piece_value(work.piece_at(cheapest_sq))
    work.remove_piece_at(cheapest_sq)
    color = not color  # opponent's turn to recapture

    # Subsequent recaptures: each ply, the side-to-move picks their cheapest
    # attacker (if any) and recaptures, gaining the value of the piece sitting
    # on the square (i.e. the last attacker, which is now the target).
    while True:
        attackers = work.attackers(color, square)
        if not attackers:
            break
        # gain[d] = value_of_piece_being_captured - gain[d-1], where the piece
        # being captured is the previous attacker now sitting on `square`.
        gains.append(last_attacker_value - gains[-1])
        cheapest_sq = min(attackers, key=lambda sq: _piece_value(work.piece_at(sq)))
        last_attacker_value = _piece_value(work.piece_at(cheapest_sq))
        work.remove_piece_at(cheapest_sq)
        color = not color

    # Minimax pull-back: at each ply, the side to move would refuse the
    # exchange if continuing loses them material (max with 0 / negate).
    for i in range(len(gains) - 1, 0, -1):
        gains[i - 1] = -max(-gains[i - 1], gains[i])
    return gains[0]


def hanging_squares(board: chess.Board, color: chess.Color) -> set[chess.Square]:
    """Set of squares where `color`'s pieces are hanging — i.e. the opponent
    can initiate a capture on that square with a positive SEE result.

    A piece is "hanging" if the opponent wins material by capturing it; this
    captures both undefended pieces under attack and defended pieces attacked
    by lower-value pieces (or pieces whose defender chain loses to the
    attacker chain). The classic queen-attacks-pawn-defended-by-pawn case
    returns SEE < 0 for the queen's side, so the pawn does NOT hang.
    """
    opponent = not color
    out: set[chess.Square] = set()
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None or piece.color != color:
            continue
        if see_capture(board, square, opponent) > 0:
            out.add(square)
    return out


async def _native_elo_move(engine: chess.engine.UciProtocol, board: chess.Board,
                           elo: int) -> chess.Move:
    """Stockfish's built-in strength limiter — only works for 1320-3190."""
    await engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
    result = await engine.play(board, chess.engine.Limit(time=MOVE_TIME_SECONDS))
    if result.move is None:
        raise chess.engine.EngineError("Stockfish returned no move")
    return result.move


def _move_is_safe(board: chess.Board, move: chess.Move, mover: chess.Color) -> bool:
    """True if applying `move` to `board` leaves no piece of `mover` hanging
    in the resulting position. This is the SEE filter's accept predicate.

    Subsumes both 'don't create a new hang' and 'address an existing threat'
    in one check: any pre-existing hanging piece that the move doesn't
    resolve (by moving it, blocking, capturing the attacker, or adding a
    defender) will still be hanging post-move and fail the check."""
    after = board.copy(stack=False)
    after.push(move)
    return not hanging_squares(after, mover)


async def _multipv_sampled_move(engine: chess.engine.UciProtocol, board: chess.Board,
                                elo: int, *, book_move: chess.Move | None = None) -> chess.Move:
    """Sub-native tier: top-N pool sampling with SEE safety filter, plus a
    random-move blend at the very bottom (Elo <400) for true-beginner play.

    1. Analyse top-MULTIPV_COUNT moves at Elo-scaled depth.
    2. If `book_move` is provided AND it's in the top-MULTIPV_COUNT, play it
       directly (opening book overrides sampling for a quality-validated
       scripted move).
    3. Otherwise truncate to top-pool_size candidates.
    4. With safety_filter_probability_for_elo: filter to candidates that don't
       leave any of the mover's pieces hanging post-move; sample uniformly.
       If none pass, fall back to the rank-1 (best) move.
    5. Otherwise: sample uniformly from the unfiltered candidate pool — the
       'this Elo would have blundered' branch.
    6. Finally, with random_blend_probability_for_elo: replace the chosen
       move with a fully random legal move (also subject to the safety
       filter if it fires). Models beginner moves Stockfish wouldn't pick.
       (Skipped when book_move is accepted — the book wins out.)
    """
    depth = multipv_depth_for_elo(elo)
    infos = await engine.analyse(
        board,
        chess.engine.Limit(depth=depth),
        multipv=MULTIPV_COUNT,
    )
    pvs = [info for info in infos if info.get("pv")]
    if not pvs:
        # Position has no usable analysis — fall back to a single-move play to
        # avoid stalling the game.
        result = await engine.play(board, chess.engine.Limit(time=MOVE_TIME_SECONDS))
        if result.move is None:
            raise chess.engine.EngineError("Stockfish returned no move")
        return result.move

    # Book-move handoff: play the scripted move if Stockfish considers it at
    # least top-10 here. Otherwise abandon and fall through to sampling.
    if book_move is not None:
        top_moves = {info["pv"][0] for info in pvs}
        if book_move in top_moves:
            return book_move

    pool_size = min(multipv_pool_size_for_elo(elo), len(pvs))
    candidates = [info["pv"][0] for info in pvs[:pool_size]]
    mover = board.turn
    filter_fires = random.random() < safety_filter_probability_for_elo(elo)

    if filter_fires:
        safe = [mv for mv in candidates if _move_is_safe(board, mv, mover)]
        chosen = random.choice(safe) if safe else pvs[0]["pv"][0]
    else:
        chosen = random.choice(candidates)

    # True-beginner random blend: at low Elo, sometimes replace the
    # Stockfish-derived pick with a fully random legal move. If the safety
    # filter fired, also apply it to the random candidates so the bot still
    # avoids the most obvious hangs even in random mode.
    if random.random() < random_blend_probability_for_elo(elo):
        legal = list(board.legal_moves)
        if legal:
            if filter_fires:
                safe_legal = [mv for mv in legal if _move_is_safe(board, mv, mover)]
                if safe_legal:
                    return random.choice(safe_legal)
            return random.choice(legal)

    return chosen


async def pick_move(fen: str, elo: int, *, book_move: chess.Move | None = None) -> chess.Move:
    """Spawn Stockfish, get one move at the target Elo, close. Per-move spawn
    keeps zero processes alive when nobody's playing chess.

    If `book_move` is provided, it's offered to the sub-native sampler which
    plays it iff Stockfish considers it top-MULTIPV_COUNT in this position.
    Native tier ignores book moves (the Elo limiter already plays strong
    book moves on its own).
    """
    board = chess.Board(fen=fen)
    elo = clamp_elo(elo)

    transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    try:
        if elo >= STOCKFISH_NATIVE_ELO_MIN:
            return await _native_elo_move(engine, board, elo)
        return await _multipv_sampled_move(engine, board, elo, book_move=book_move)
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()
