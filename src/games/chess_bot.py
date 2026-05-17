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
MULTIPV_COUNT = 12

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
#
# Depth is intentionally flat at 2 across Elo 400-500 (a narrow plateau at
# the bottom of the curve) — the strength gradient in that band comes from
# the random-blend taper and the tightening safety filter, NOT from deeper
# search. From Elo 600 upward depth ramps toward native (depth 14) by
# Elo 1300 so the handoff to UCI_Elo 1320 is monotonic.
#
# Pool is similarly flat at 10 across Elo 400-700, then ramps down to 3 by
# Elo 1200+. MULTIPV_COUNT must stay ≥ the largest pool anchor (currently 12).
_DEPTH_ANCHORS: list[tuple[int, int]] = [
    (100, 1),     # Immediate captures only (depth-1 + quiescence)
    (300, 2),     # Hanging pieces, mate-in-1
    (400, 2),     # Frozen plateau start
    (500, 2),     # Frozen plateau end
    (600, 4),     # Simple 1-move tactics
    (700, 4),
    (900, 7),     # Most 2-3 move tactics
    (1000, 8),    # 3-move tactics, simple mating attacks
    (1100, 10),   # 4-move tactics
    (1200, 12),   # 5-move forced sequences
    (1300, 14),   # Strong club-player tactics — boundary with native tier
]

_POOL_SIZE_ANCHORS: list[tuple[int, int]] = [
    (100, 12),
    (400, 10),    # Frozen plateau start
    (700, 10),    # Frozen plateau end — same as Elo 400
    (800, 8),
    (900, 7),
    (1000, 5),
    (1100, 4),
    (1200, 3),
    (1300, 3),
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
    (1300, 0.975),
]

# Probability of taking a hanging opponent piece when it's available in the
# top-N candidate pool. Models "spotting free material" as a fundamental
# skill that improves with Elo — even beginners notice an undefended queen
# most of the time. High floor (0.75 at Elo 100) since taking free pieces
# is the easiest tactical pattern to spot.
_TAKE_HANGING_ANCHORS: list[tuple[int, float]] = [
    (100, 0.75),
    (400, 0.85),
    (600, 0.90),
    (800, 0.95),
    (1000, 0.99),
    (1300, 0.99),
]

# Probability of replacing Stockfish's suggestion with a fully random legal
# move. Real beginners play moves that aren't even in Stockfish's top-10 —
# odd rook lifts, edge-pawn shuffles, etc. Strong at the bottom, tapering
# to a small-but-nonzero rate through 400-600, fully off by Elo 700.
_RANDOM_BLEND_ANCHORS: list[tuple[int, float]] = [
    (100, 0.80),
    (200, 0.50),
    (300, 0.30),
    (400, 0.15),
    (500, 0.08),
    (600, 0.03),
    (700, 0.0),
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
    0.80 at Elo 100, decays to 0 by Elo 700 and stays 0 above that."""
    return _interp_float(_RANDOM_BLEND_ANCHORS, elo)


def take_hanging_probability_for_elo(elo: int) -> float:
    """Probability that the bot will actually take an opponent's hanging
    piece when one is present in the top-N candidate pool. High at all
    Elos (0.75 floor) since spotting a free piece is easy; ramps to 0.99
    by Elo 1000+. Independent of the safety filter — even beginners take
    free material most of the time."""
    return _interp_float(_TAKE_HANGING_ANCHORS, elo)


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
    """True if applying `move` to `board` leaves the JUST-MOVED piece not
    hanging in the resulting position. This is the SEE filter's accept
    predicate.

    Restricted to the moving piece's destination square (rather than every
    piece on the board) so the bot models 'don't walk into an attack' but
    NOT 'spot every uncovered defender or discovered attack' — those are
    higher-skill mistakes the lower-Elo bot is expected to miss. Pre-
    existing hangs the move doesn't resolve also aren't a reason to reject
    a move here; the take-hanging branch (which also restricts to the
    opponent's last-moved piece) is the symmetric structure for offense."""
    after = board.copy(stack=False)
    after.push(move)
    # Castling moves the king two squares — the king is the "moving piece"
    # whose safety we care about; its destination is move.to_square.
    return move.to_square not in hanging_squares(after, mover)


def _capture_of_hanging_piece(
    board: chess.Board, candidates: list[chess.Move], mover: chess.Color,
    last_to_square: chess.Square | None,
) -> chess.Move | None:
    """If any candidate move captures the OPPONENT'S LAST-MOVED PIECE on a
    hanging square (per SEE), return that move. Restricting to the last-
    moved piece models the most common human mistake pattern: the opponent
    blunders by moving a piece into attack, and the bot punishes it. Pre-
    existing hangs (pieces the opponent failed to defend on earlier moves)
    are NOT caught here — a real player at this Elo wouldn't have spotted
    them either.

    `last_to_square` is the destination square of the opponent's most recent
    move (caller passes None when there's no prior move). Returns None if:
    - no last move (bot moves first),
    - that square isn't hanging,
    - or no candidate in the top-N pool captures it.
    """
    if last_to_square is None:
        return None
    opp_hangs = hanging_squares(board, not mover)
    if last_to_square not in opp_hangs:
        return None
    target = board.piece_at(last_to_square)
    if target is None:
        return None
    for mv in candidates:
        if mv.to_square == last_to_square:
            return mv
    return None


async def _multipv_sampled_move(engine: chess.engine.UciProtocol, board: chess.Board,
                                elo: int, *,
                                book_move: chess.Move | None = None,
                                last_to_square: chess.Square | None = None) -> chess.Move:
    """Sub-native tier: top-N pool sampling with SEE safety filter, plus a
    random-move blend at the very bottom (Elo <400) for true-beginner play.

    1. Analyse top-MULTIPV_COUNT moves at Elo-scaled depth.
    2. If `book_move` is provided AND it's in the top-MULTIPV_COUNT, play it
       directly (opening book overrides sampling for a quality-validated
       scripted move).
    3. Truncate to top-pool_size candidates.
    4. Take-hanging check: if the opponent has a hanging piece AND a top-N
       candidate captures it, play that capture with take_hanging_probability.
       Models the basic skill of "spotting free material" — runs before
       sampling/blending because it represents the most fundamental
       tactical pattern.
    5. With safety_filter_probability_for_elo: filter to candidates that don't
       leave any of the mover's pieces hanging post-move; sample uniformly.
       If none pass, fall back to the rank-1 (best) move.
    6. Otherwise: sample uniformly from the unfiltered candidate pool — the
       'this Elo would have blundered' branch.
    7. Finally, with random_blend_probability_for_elo: replace the chosen
       move with a fully random legal move (also subject to the safety
       filter if it fires). Models beginner moves Stockfish wouldn't pick.
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

    # Take-hanging check: if the opponent's just-moved piece is hanging AND
    # one of our top-N candidates captures it, take it with high probability.
    # Restricting to the LAST-MOVED enemy piece models the common pattern of
    # "I noticed your blunder" — pre-existing hangs the opponent failed to
    # fix earlier are not caught here.
    take_hanging = _capture_of_hanging_piece(board, candidates, mover, last_to_square)
    if take_hanging is not None and random.random() < take_hanging_probability_for_elo(elo):
        return take_hanging

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


async def pick_move(
    fen: str, elo: int, *,
    book_move: chess.Move | None = None,
    last_move_uci: str | None = None,
) -> chess.Move:
    """Spawn Stockfish, get one move at the target Elo, close. Per-move spawn
    keeps zero processes alive when nobody's playing chess.

    If `book_move` is provided, it's offered to the sub-native sampler which
    plays it iff Stockfish considers it top-MULTIPV_COUNT in this position.
    Native tier ignores book moves (the Elo limiter already plays strong
    book moves on its own).

    If `last_move_uci` is provided, it tells the take-hanging check which
    square the opponent's last-moved piece landed on (so the bot only
    punishes fresh blunders, not pre-existing hangs). Skipped silently if
    the UCI doesn't parse.
    """
    board = chess.Board(fen=fen)
    last_to_square: chess.Square | None = None
    if last_move_uci:
        try:
            last_to_square = chess.Move.from_uci(last_move_uci).to_square
        except (ValueError, chess.InvalidMoveError):
            last_to_square = None
    elo = clamp_elo(elo)

    transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    try:
        if elo >= STOCKFISH_NATIVE_ELO_MIN:
            return await _native_elo_move(engine, board, elo)
        return await _multipv_sampled_move(
            engine, board, elo,
            book_move=book_move, last_to_square=last_to_square,
        )
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()
