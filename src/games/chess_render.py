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

# Override python-chess's default subtle yellow lastmove tint. Two palettes:
# blue for ordinary moves, red for moves that captured a piece (signals
# 'something died here'). python-chess uses separate keys for light- vs
# dark-square tints so both colors are set in each palette.
_LASTMOVE_BLUE = {
    "square light lastmove": "#5db0ff",  # vivid blue on light squares
    "square dark lastmove": "#1e72d4",   # darker blue on dark squares
}
_LASTMOVE_RED = {
    "square light lastmove": "#ff7a7a",  # vivid red on light squares
    "square dark lastmove": "#c63838",   # darker red on dark squares
}


def render_board_png(
    board: chess.Board,
    *,
    orientation: bool = chess.WHITE,
    last_move: chess.Move | None = None,
    last_move_was_capture: bool = False,
    threat_squares: set[chess.Square] | None = None,
    size: int = DEFAULT_SIZE,
) -> bytes:
    if cairosvg is None:
        raise RuntimeError(
            f"cairosvg unavailable ({_CAIROSVG_IMPORT_ERROR!r}); install libcairo2 in the runtime image."
        )
    check_square = board.king(board.turn) if board.is_check() else None
    colors = _LASTMOVE_RED if last_move_was_capture else _LASTMOVE_BLUE
    svg = chess.svg.board(
        board,
        orientation=orientation,
        lastmove=last_move,
        check=check_square,
        size=size,
        colors=colors,
    )
    if threat_squares:
        svg = _inject_threat_overlays(svg, threat_squares, orientation)
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=size)


def _inject_threat_overlays(
    svg: str, squares: set[chess.Square], orientation: chess.Color,
) -> str:
    """Post-process a python-chess SVG to add red radial-gradient overlays
    on the given squares. Uses the same `check_gradient` def python-chess
    already emits when `check=` is passed; we just add extra <rect> elements
    pointing at it.

    Layered RIGHT AFTER the `<defs>` block closes (before any pieces are
    drawn), so the overlays sit BENEATH the pieces — pieces remain readable.
    """
    # python-chess uses these SVG constants. Keep in sync if upstream changes.
    SQUARE_SIZE = 45  # noqa: N806 — matches chess.svg constant name
    BOARD_OFFSET = 15  # padding before the playable squares begin
    # Make sure the gradient def is present. python-chess only emits it when
    # `check=` was passed; if our caller didn't, we inject our own.
    if "check_gradient" not in svg:
        gradient_def = (
            '<radialGradient id="check_gradient" r="0.5">'
            '<stop offset="0%" stop-color="#ff0000" stop-opacity="1.0" />'
            '<stop offset="50%" stop-color="#e70000" stop-opacity="1.0" />'
            '<stop offset="100%" stop-color="#9e0000" stop-opacity="0.0" />'
            '</radialGradient>'
        )
        svg = svg.replace("</defs>", gradient_def + "</defs>", 1)

    rects = []
    for sq in squares:
        file_idx = chess.square_file(sq)
        rank_idx = chess.square_rank(sq)
        if orientation == chess.WHITE:
            x = BOARD_OFFSET + file_idx * SQUARE_SIZE
            y = BOARD_OFFSET + (7 - rank_idx) * SQUARE_SIZE
        else:
            x = BOARD_OFFSET + (7 - file_idx) * SQUARE_SIZE
            y = BOARD_OFFSET + rank_idx * SQUARE_SIZE
        rects.append(
            f'<rect x="{x}" y="{y}" width="{SQUARE_SIZE}" height="{SQUARE_SIZE}"'
            f' class="threat" fill="url(#check_gradient)" />'
        )

    # Inject after the <defs>...</defs> block so the rects sit beneath pieces.
    overlay = "".join(rects)
    return svg.replace("</defs>", "</defs>" + overlay, 1)
