from __future__ import annotations

import os
import random
import shutil

import chess
import chess.engine


# Stockfish's UCI_Elo option is clamped to this range internally. Below 1320
# Stockfish has no real "weak" setting — even Skill Level 0 plays at roughly
# native floor (~1350 Elo) because it's still a full alpha-beta search. To
# actually play weaker than 1320 we run Stockfish at Skill Level 0 and then,
# with linearly-interpolated probability, replace its move with a random
# legal move. At Elo 100 the move is almost always random; at Elo 1320 the
# move is always Stockfish.
STOCKFISH_NATIVE_ELO_MIN = 1320
STOCKFISH_NATIVE_ELO_MAX = 3190

# What we accept from users via !chess @Bot <elo>.
ELO_MIN = 100
ELO_MAX = STOCKFISH_NATIVE_ELO_MAX
ELO_DEFAULT = 1320

# Per-move think time. 500ms at any Elo gives Stockfish plenty of strength
# above the floor while keeping bot responses snappy on Discord.
MOVE_TIME_SECONDS = 0.5

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
    """Probability of replacing Stockfish's move with a random legal move.
    Linear: 1.0 at ELO_MIN, 0.0 at STOCKFISH_NATIVE_ELO_MIN, clamped outside."""
    if elo >= STOCKFISH_NATIVE_ELO_MIN:
        return 0.0
    if elo <= ELO_MIN:
        return 1.0
    span = STOCKFISH_NATIVE_ELO_MIN - ELO_MIN  # 1220
    return (STOCKFISH_NATIVE_ELO_MIN - elo) / span


async def pick_move(fen: str, elo: int) -> chess.Move:
    """Spawn Stockfish, get one move at the target Elo, close. Per-move spawn
    keeps zero processes alive when nobody's playing chess."""
    board = chess.Board(fen=fen)
    elo = clamp_elo(elo)

    transport, engine = await chess.engine.popen_uci(_resolve_stockfish_path())
    try:
        if elo >= STOCKFISH_NATIVE_ELO_MIN:
            await engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
        else:
            # Stockfish at its weakest. The actual weakening below 1320 comes
            # from the random-move blend after the engine returns.
            await engine.configure({"Skill Level": 0})
        result = await engine.play(board, chess.engine.Limit(time=MOVE_TIME_SECONDS))
        if result.move is None:
            raise chess.engine.EngineError("Stockfish returned no move")
        engine_move = result.move
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()

    p_random = random_move_probability(elo)
    if p_random > 0 and random.random() < p_random:
        legal_moves = list(board.legal_moves)
        if legal_moves:
            return random.choice(legal_moves)
    return engine_move
