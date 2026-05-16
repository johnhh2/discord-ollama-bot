from __future__ import annotations

import os

import chess
import chess.engine


# Stockfish's UCI_Elo option is clamped to this range internally; below 1320 we
# fall through to its Skill Level option (0-20) which weakens differently.
STOCKFISH_NATIVE_ELO_MIN = 1320
STOCKFISH_NATIVE_ELO_MAX = 3190

# What we accept from users via !chess @Bot <elo>.
ELO_MIN = 100
ELO_MAX = STOCKFISH_NATIVE_ELO_MAX
ELO_DEFAULT = 1320

# Per-move think time. 500ms at any Elo gives Stockfish plenty of strength
# above the floor while keeping bot responses snappy on Discord.
MOVE_TIME_SECONDS = 0.5

# Path lookup: STOCKFISH_PATH env var overrides; otherwise PATH lookup finds
# the apt-installed binary in the Docker image (Debian installs at
# /usr/games/stockfish but it's on the default PATH).
_STOCKFISH_BINARY = os.environ.get("STOCKFISH_PATH", "stockfish")


def clamp_elo(elo: int) -> int:
    if elo < ELO_MIN:
        return ELO_MIN
    if elo > ELO_MAX:
        return ELO_MAX
    return elo


def skill_level_for_elo(elo: int) -> int:
    """Linear-ish map from sub-native Elo (100..1319) to Skill Level (0..20).
    Used when target Elo is below Stockfish's UCI_Elo floor."""
    elo = max(ELO_MIN, min(STOCKFISH_NATIVE_ELO_MIN - 1, elo))
    span = STOCKFISH_NATIVE_ELO_MIN - 1 - ELO_MIN  # 1219
    return round((elo - ELO_MIN) * 20 / span)


async def pick_move(fen: str, elo: int) -> chess.Move:
    """Spawn Stockfish, get one move at the target Elo, close. Per-move spawn
    keeps zero processes alive when nobody's playing chess."""
    board = chess.Board(fen=fen)
    elo = clamp_elo(elo)

    transport, engine = await chess.engine.popen_uci(_STOCKFISH_BINARY)
    try:
        if elo >= STOCKFISH_NATIVE_ELO_MIN:
            await engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
        else:
            await engine.configure({"Skill Level": skill_level_for_elo(elo)})
        result = await engine.play(board, chess.engine.Limit(time=MOVE_TIME_SECONDS))
        if result.move is None:
            raise chess.engine.EngineError("Stockfish returned no move")
        return result.move
    finally:
        try:
            await engine.quit()
        except Exception:
            transport.close()
