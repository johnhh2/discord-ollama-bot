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

# Blue radial gradient mirroring python-chess's CHECK_GRADIENT (which is red).
# Applied to the destination square of the last move to give a vivid visual
# cue equivalent to the check highlight but in blue.
_LASTMOVE_GRADIENT_SVG = (
    '<radialGradient id="lastmove_gradient" r="0.5">'
    '<stop offset="0%" stop-color="#3da5ff" stop-opacity="1.0" />'
    '<stop offset="50%" stop-color="#1c7fe0" stop-opacity="1.0" />'
    '<stop offset="100%" stop-color="#0a4080" stop-opacity="0.0" />'
    '</radialGradient>'
)


def _square_xy(square: chess.Square, orientation: bool) -> tuple[int, int]:
    """Top-left pixel coordinates of `square` in the python-chess SVG board.
    Mirrors python-chess's own coordinate computation (SQUARE_SIZE=45,
    MARGIN=20, board oriented for `orientation`)."""
    file_index = chess.square_file(square)
    rank_index = chess.square_rank(square)
    x = (file_index if orientation else 7 - file_index) * chess.svg.SQUARE_SIZE + chess.svg.MARGIN
    y = (7 - rank_index if orientation else rank_index) * chess.svg.SQUARE_SIZE + chess.svg.MARGIN
    return x, y


def _inject_lastmove_highlight(svg: str, last_move: chess.Move, orientation: bool) -> str:
    """Inject the blue lastmove gradient definition and an overlay rect on
    the move's destination square — same visual treatment as the red check
    highlight but in blue."""
    x, y = _square_xy(last_move.to_square, orientation)
    size = chess.svg.SQUARE_SIZE
    overlay_rect = (
        f'<rect x="{x}" y="{y}" width="{size}" height="{size}" '
        f'class="lastmove" fill="url(#lastmove_gradient)" />'
    )
    # Place the gradient inside the existing <defs> block (python-chess
    # always emits one for the piece definitions). Place the rect just
    # before </svg> so it sits on top of the board but under any future
    # overlays python-chess might add.
    svg = svg.replace("</defs>", _LASTMOVE_GRADIENT_SVG + "</defs>", 1)
    svg = svg.replace("</svg>", overlay_rect + "</svg>", 1)
    return svg


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
    if last_move is not None:
        svg = _inject_lastmove_highlight(svg, last_move, orientation)
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=size)
