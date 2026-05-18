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


# Vivid red tint applied to threatened squares by !chessthreats. Uses
# python-chess's native `fill=` overlay mechanism (same one it uses for
# every board square) — no SVG post-processing.
_THREAT_FILL_COLOR = "#ff5050"

# Arrow colors for the !chessthreats overlay. Light shade = piece is white,
# dark shade = piece is black (matches the universal chess convention that
# white = light, black = dark). Attackers are red, defenders are green.
_ATTACKER_ARROW_COLOR = {
    chess.WHITE: "#ff6060",  # bright red — white attacker
    chess.BLACK: "#a00000",  # dark red — black attacker
}
_DEFENDER_ARROW_COLOR = {
    chess.WHITE: "#60ff60",  # bright green — white defender
    chess.BLACK: "#007000",  # dark green — black defender
}


def _threat_arrows(board: chess.Board, threat_squares: set[chess.Square]) -> list:
    """For each hanging square, build arrows from every attacker (colored by
    attacker's side, red shades) and every defender (green shades). Defender
    arrows let the admin see WHY SEE judges the piece hanging — usually
    there's a defender chain that doesn't break even with the attackers."""
    arrows: list = []
    for hang_sq in threat_squares:
        victim = board.piece_at(hang_sq)
        if victim is None:
            continue
        attacker_color = not victim.color
        defender_color = victim.color
        for attacker_sq in board.attackers(attacker_color, hang_sq):
            arrows.append(chess.svg.Arrow(
                attacker_sq, hang_sq,
                color=_ATTACKER_ARROW_COLOR[attacker_color],
            ))
        for defender_sq in board.attackers(defender_color, hang_sq):
            # Skip self-defends (the piece itself shows up as defending its
            # own square — not actually a defender).
            if defender_sq == hang_sq:
                continue
            arrows.append(chess.svg.Arrow(
                defender_sq, hang_sq,
                color=_DEFENDER_ARROW_COLOR[defender_color],
            ))
    return arrows


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
    fill: dict[chess.Square, str] = {}
    arrows: list = []
    if threat_squares:
        for sq in threat_squares:
            fill[sq] = _THREAT_FILL_COLOR
        arrows = _threat_arrows(board, threat_squares)
    svg = chess.svg.board(
        board,
        orientation=orientation,
        lastmove=last_move,
        check=check_square,
        size=size,
        colors=colors,
        fill=fill,
        arrows=arrows,
    )
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=size)
