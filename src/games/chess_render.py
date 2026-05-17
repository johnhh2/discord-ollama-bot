from __future__ import annotations

import chess
import chess.svg


# cairosvg loads a native libcairo at import time, which may be missing on dev
# machines (Windows without the dll, slim Linux without libcairo2). Defer the
# import error to render time so the rest of the game module still loads.
try:
    import cairosvg  # type: ignore
    _CAIROSVG_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover
    cairosvg = None  # type: ignore
    _CAIROSVG_IMPORT_ERROR = _e


DEFAULT_SIZE = 720


def render_board_png(
    board: chess.Board,
    *,
    orientation: bool = chess.WHITE,
    last_move: chess.Move | None = None,
    size: int = DEFAULT_SIZE,
) -> bytes:
    if cairosvg is None:
        raise RuntimeError(
            f"cairosvg unavailable ({_CAIROSVG_IMPORT_ERROR!r}); install libcairo2 in the runtime image."
        )
    check_square = board.king(board.turn) if board.is_check() else None
    svg = chess.svg.board(
        board,
        orientation=orientation,
        lastmove=last_move,
        check=check_square,
        size=size,
    )
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=size)
