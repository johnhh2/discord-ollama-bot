from __future__ import annotations

import logging
import os
import random
import shutil
from pathlib import Path

import chess
import chess.engine


# Rating scale: bot Elo labels are LICHESS-SCALE. Maia networks are trained
# on Lichess games at their target rating, so "Maia 1500" means "moves like
# a Lichess 1500" by construction, and the sub-Maia tier inherits that scale
# by degrading downward from Maia 1100. chess.com ratings run ~200-400 lower
# below 2000, so a chess.com-600 player should pick roughly a 900 bot. The
# native tier (2000+) is only approximately on-scale: Stockfish's UCI_Elo is
# anchored to CCRL blitz (an engine pool, hardware/TC-dependent, compressed
# near the top) — but human rating scales converge above ~2000 anyway, so
# treat those labels as "roughly this strong". The post-game analysis
# estimator (chess_analysis.py) is calibrated against these same tiers, so
# its est. Elo agrees with this scale by construction.
#
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
    (400, 10),
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
    (400, 0.02),
    (700, 0.02),
    (1000, 0.01),
]

# Base probability that the bot NOTICES a hanging piece (its own or the
# opponent's) and re-routes its move to address it. Linear from 0.40 at
# Elo 100 to 0.95 at Elo 1000. Total notice rate adds a value-based bonus
# (queens get noticed more than pawns); see _PIECE_NOTICE_BONUS below.
_NOTICE_BASE_ANCHORS: list[tuple[int, float]] = [
    (100, 0.25),
    (400, 0.55),
    (700, 0.70),
    (1000, 0.85),
]

# Bonus added to the base notice probability per piece type. Models the
# real-beginner pattern that a hanging queen is way more obvious than a
# hanging pawn — even Elo 100 humans rarely walk a queen into attack
# without noticing. Combined notice rate is capped at 0.99.
_PIECE_NOTICE_BONUS: dict[chess.PieceType, float] = {
    chess.PAWN: 0.0,
    chess.KNIGHT: 0.25,
    chess.BISHOP: 0.25,
    chess.ROOK: 0.45,
    chess.QUEEN: 0.60,
    chess.KING: 0.0,  # king "hanging" means check — handled by chess rules
}

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


def engine_label_for_elo(elo: int) -> str:
    """Human-readable engine name for the given Elo, used in chess match
    embeds and game-over reports. Reflects which tier actually plays:
    'Sub-Maia' for 100-1000, 'Maia' for 1100-1900, 'Stockfish' for 2000+."""
    if elo >= STOCKFISH_NATIVE_ELO_MIN:
        return "Stockfish"
    if elo >= MAIA_ELO_MIN:
        return "Maia"
    return "Sub-Maia"


def engine_name_with_elo(elo: int) -> str:
    """e.g. 'Maia (1500 Elo)' for use in match embeds and player labels."""
    return f"{engine_label_for_elo(elo)} ({elo} Elo)"


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


def notice_probability_for_elo_and_piece(elo: int, piece_type: chess.PieceType) -> float:
    """Probability that the bot notices a hanging piece of the given type and
    re-routes its move to address it. Combines the Elo-base rate with a piece-
    value bonus so queens are noticed more reliably than pawns even at low Elo.
    Capped at 0.99."""
    base = _interp_float(_NOTICE_BASE_ANCHORS, elo)
    bonus = _PIECE_NOTICE_BONUS.get(piece_type, 0.0)
    return min(0.99, base + bonus)


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


def hanging_squares(board: chess.Board, color: chess.Color) -> set[chess.Square]:
    """Set of squares where `color`'s pieces are sitting on a SEE-losing
    square — i.e. the opponent gains material by initiating a capture there.

    Used by the threat-awareness check to detect both:
    - Own pieces in danger (need to defend / move / capture attacker / block)
    - Opponent pieces that could be captured for free (take-hanging)
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


def _most_valuable_hanging(board: chess.Board, color: chess.Color) -> chess.Square | None:
    """The single highest-value hanging square for `color`, or None if no
    pieces hang. Used to pick which threat the bot is most likely to notice."""
    squares = hanging_squares(board, color)
    if not squares:
        return None
    return max(squares, key=lambda sq: _piece_value(board.piece_at(sq)))


def _move_addresses_self_hang(board: chess.Board, move: chess.Move,
                              hang_square: chess.Square) -> bool:
    """True iff applying `move` to `board` results in `hang_square` no longer
    being hanging for the side that just moved. Covers all defensive options
    in one check: piece moved away, attacker captured, defender added, line
    blocked."""
    mover = board.turn
    after = board.copy(stack=False)
    after.push(move)
    # If the piece itself moved off the hanging square, the original threat
    # is gone (whether or not a new threat exists on the destination).
    if move.from_square == hang_square:
        return True
    # Otherwise the piece is still on hang_square. Check whether it's still
    # SEE-losing for the mover (i.e. opponent can still win material there).
    return see_capture(after, hang_square, not mover) <= 0


def _move_takes_opp_hang(move: chess.Move, hang_square: chess.Square) -> bool:
    """True iff `move` captures the opponent's hanging piece on `hang_square`."""
    return move.to_square == hang_square


def _find_see_losing_move(board: chess.Board, exclude: chess.Move | None = None) -> chess.Move | None:
    """Walk legal moves and return a SEE-losing one weighted INVERSELY by
    the value of the piece being dropped.

    Weighting by 1/piece_value matches the real-beginner pattern: hanging
    a pawn happens often, hanging a queen rarely. Without this weighting,
    a uniform-random pick treats queen-drops and pawn-drops equally, which
    makes the bot hang queens on early moves at any Elo (not realistic).

    Used by the extra-blunder injector. Excludes `exclude` (typically
    Maia's chosen move) so the injector actually changes the move.

    Returns None if no SEE-losing move exists (rare in middlegames; common
    in simplified endgames). Caller falls back to keeping Maia's pick."""
    mover = board.turn
    opp = not mover
    losing: list[chess.Move] = []
    weights: list[float] = []
    for mv in board.legal_moves:
        if exclude is not None and mv == exclude:
            continue
        # The "dropped piece" is the one ON `mv.from_square` BEFORE the move
        # — that's what would walk into the losing exchange on mv.to_square.
        piece = board.piece_at(mv.from_square)
        if piece is None:
            continue
        # SEE from the opponent's perspective: if they would gain material by
        # capturing on our move's destination square AFTER we move there,
        # that move drops material.
        after = board.copy(stack=False)
        after.push(mv)
        if see_capture(after, mv.to_square, opp) > 0:
            losing.append(mv)
            # Weight inversely by piece value. Pawn (1) → weight 1.0,
            # knight/bishop (3) → 0.33, rook (5) → 0.2, queen (9) → 0.11.
            # King hangs shouldn't be reachable here (illegal move), but
            # guard with the same formula via _piece_value.
            weights.append(1.0 / _piece_value(piece))
    if not losing:
        return None
    return random.choices(losing, weights=weights, k=1)[0]


# Sub-Maia tier with mate-check enabled. Below 300, the bot is expected to
# walk into mate-in-1 occasionally (authentic beginner behavior). From 300+
# we filter out candidates that allow immediate mate. From 700+ we also
# spawn Stockfish at depth 4 to catch mate-in-2 setups.
_MATE_CHECK_MIN_ELO = 300
_MATE_TWO_PLY_MIN_ELO = 700
_MATE_TWO_PLY_STOCKFISH_DEPTH = 4


def _move_allows_mate_in_one(board: chess.Board, move: chess.Move) -> bool:
    """True iff playing `move` lets the opponent deliver mate in 1.
    Pure Python; ~microseconds per call (scans opponent's legal moves
    after our push). Used at Elo 300+ to filter out candidates that
    walk into the simplest tactical losses."""
    after = board.copy(stack=False)
    after.push(move)
    if after.is_game_over():
        # Move ended the game (mate, stalemate, etc.) — opponent has no reply,
        # so by definition no mate-in-1 reply exists.
        return False
    for reply in after.legal_moves:
        with_reply = after.copy(stack=False)
        with_reply.push(reply)
        if with_reply.is_checkmate():
            return True
    return False


async def _move_allows_mate_in_two(board: chess.Board, move: chess.Move) -> bool:
    """True iff playing `move` lets the opponent force mate within 2 plies
    (2 full opponent moves with our move in between). Implemented via
    Stockfish at depth 4 — that's deep enough to find any mate-in-2
    instantly.

    Returns False on engine failure (no Stockfish) — we'd rather let the
    move through than block on engine unavailability."""
    after = board.copy(stack=False)
    after.push(move)
    if after.is_game_over():
        return False
    try:
        transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    except (chess.engine.EngineError, FileNotFoundError, OSError):
        return False
    try:
        # Always pass multipv=1 so the return shape is always a list (real
        # engines return a dict when multipv is unset, list when it's set —
        # forcing the list shape simplifies the test fake too).
        infos = await engine.analyse(
            after,
            chess.engine.Limit(depth=_MATE_TWO_PLY_STOCKFISH_DEPTH),
            multipv=1,
        )
        if not infos:
            return False
        info = infos[0]
        score = info.get("score")
        if score is None:
            return False
        # Opponent's POV after our push. A mate score from their POV with
        # 1 or 2 plies means they have a forced mate-in-1-or-2 from here.
        # python-chess's PovScore.relative is relative to side-to-move
        # (the opponent here).
        mate = score.relative.mate()
        if mate is None:
            return False
        # mate > 0 means side-to-move (opponent) mates in `mate` moves.
        # mate <= 2 means within our threshold.
        return 0 < mate <= 2
    except chess.engine.EngineError:
        return False
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()


def _filter_mate_in_one(board: chess.Board, candidates: list[chess.Move]) -> list[chess.Move]:
    """Return the subset of candidates that DON'T allow mate-in-1. If every
    candidate allows mate (we're in a lost position), return the original
    list unchanged so the caller still has something to play."""
    safe = [mv for mv in candidates if not _move_allows_mate_in_one(board, mv)]
    return safe if safe else candidates


async def _filter_mate_in_two(board: chess.Board, candidates: list[chess.Move]) -> list[chess.Move]:
    """Return the subset of candidates that DON'T allow mate within 2 plies.
    Same fallback behavior as _filter_mate_in_one when nothing survives."""
    safe: list[chess.Move] = []
    for mv in candidates:
        allows = await _move_allows_mate_in_two(board, mv)
        if not allows:
            safe.append(mv)
    return safe if safe else candidates


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
        # No --backend flag: lc0 auto-selects the backend it was compiled
        # with. Our Docker image builds lc0 with OpenBLAS (CPU). If you want
        # to force a specific backend at runtime, add e.g. --backend=blas.
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


def _apply_threat_awareness(
    board: chess.Board, candidates: list[chess.Move], sampled: chess.Move,
    elo: int,
) -> chess.Move:
    """Apply the threat-awareness check: with Elo-dependent probability,
    re-route the sampled move to address a hanging piece (own or opponent's)
    that the sampled move ignored.

    Logic mirrors the way real players notice threats:
    1. If the bot's own most-valuable hanging piece isn't addressed by the
       sampled move, with P(notice | piece value, Elo): swap to a candidate
       that addresses it. (Defensive.)
    2. If the opponent has a hanging piece a candidate could capture and
       the sampled move ignores it, with P(notice | piece value, Elo):
       swap to the capture. (Offensive.)

    Notice rate scales with the hanging piece's value (queens are noticed
    more than pawns) and with Elo (1000 noticed almost everything).

    Fallback strategy when the safety dice fires but no candidate in the
    pool addresses the threat: narrow the pool to top-N/2 (Maia's most
    confident moves) and pick from there. Models 'I really see this — even
    if it's not my usual style, I'll play tighter.'
    """
    mover = board.turn

    # Defensive: address our own most-valuable hanging piece.
    own_hang = _most_valuable_hanging(board, mover)
    if own_hang is not None:
        piece = board.piece_at(own_hang)
        piece_type = piece.piece_type if piece is not None else chess.PAWN
        p_notice = notice_probability_for_elo_and_piece(elo, piece_type)
        if not _move_addresses_self_hang(board, sampled, own_hang):
            if random.random() < p_notice:
                addressing = [
                    mv for mv in candidates
                    if _move_addresses_self_hang(board, mv, own_hang)
                ]
                if addressing:
                    sampled = random.choice(addressing)
                else:
                    # No pool candidate addresses — narrow to top-N/2.
                    half = max(1, len(candidates) // 2)
                    narrowed = candidates[:half]
                    sampled = random.choice(narrowed) if len(narrowed) > 1 else narrowed[0]

    # Offensive: grab opponent's most-valuable hanging piece if available
    # in the candidate pool.
    opp_hang = _most_valuable_hanging(board, not mover)
    if opp_hang is not None:
        target = board.piece_at(opp_hang)
        target_type = target.piece_type if target is not None else chess.PAWN
        p_notice = notice_probability_for_elo_and_piece(elo, target_type)
        if not _move_takes_opp_hang(sampled, opp_hang):
            capturing = [mv for mv in candidates if _move_takes_opp_hang(mv, opp_hang)]
            if capturing and random.random() < p_notice:
                sampled = random.choice(capturing)

    return sampled


async def _sub_maia_move(board: chess.Board, elo: int) -> chess.Move:
    """Elo 100-1000 tier: sample uniformly from Maia 1100's top-N policy
    moves, layer on a threat-awareness check, then optionally inject a
    forced blunder.

    1. Ask Maia 1100 for its top-pool_size moves (each one is a real move
       1100-rated humans actually play, just less common at the tail).
    2. Filter the pool for mate-avoidance:
         - Elo 300-600: drop candidates that allow opponent mate-in-1.
         - Elo 700-1000: drop candidates that allow opponent mate-in-2
           (Stockfish at depth 4).
         - Elo 100-200: no filter — real beginners walk into mate.
       If all candidates allow mate, keep the original list (lost position).
    3. Sample one uniformly from the filtered pool.
    4. Apply threat-awareness: with P(notice) based on Elo and piece value,
       re-route to address a hanging piece (own or opponent's) the sampled
       move ignored.
    5. With extra_blunder_probability_for_elo (only nonzero below Elo 500):
       swap the chosen move for a SEE-losing alternative if one exists —
       but verify it doesn't also allow mate.
    6. Return.
    """
    pool_size = maia_pool_size_for_elo(elo)
    top_n = await _maia_top_n(board, SUB_MAIA_BASELINE_ELO, pool_size)
    # Clamp in case Maia returned fewer than pool_size in tight positions.
    candidates = top_n[:pool_size]

    # Mate-check filter (skipped when we're already in check — chess rules
    # already constrain legal moves to address it).
    if not board.is_check():
        if elo >= _MATE_TWO_PLY_MIN_ELO:
            candidates = await _filter_mate_in_two(board, candidates)
        elif elo >= _MATE_CHECK_MIN_ELO:
            candidates = _filter_mate_in_one(board, candidates)

    move = random.choice(candidates) if len(candidates) > 1 else candidates[0]

    move = _apply_threat_awareness(board, candidates, move, elo)

    if random.random() < extra_blunder_probability_for_elo(elo):
        blunder = _find_see_losing_move(board, exclude=move)
        # If a blunder candidate exists, make sure it doesn't also allow
        # mate when the mate-check tier is active.
        if blunder is not None:
            allows_mate = False
            if not board.is_check():
                if elo >= _MATE_TWO_PLY_MIN_ELO:
                    allows_mate = await _move_allows_mate_in_two(board, blunder)
                elif elo >= _MATE_CHECK_MIN_ELO:
                    allows_mate = _move_allows_mate_in_one(board, blunder)
            if not allows_mate:
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
