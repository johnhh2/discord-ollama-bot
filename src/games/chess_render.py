from __future__ import annotations

import json
import logging
from pathlib import Path

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

# Piece sets. The default is cburnett — the set python-chess ships built-in
# (chess.svg.PIECES). The others are vendored under chess_piece_sets/ as
# JSON files mapping piece symbols to 45x45 <g> fragments, converted from
# lichess's per-piece SVGs (see chess_piece_sets/LICENSES.md for provenance
# and the transformations applied). Nothing selects a non-default set yet;
# the shop only sells the unlocks.
DEFAULT_PIECE_SET = "cburnett"
PIECE_SETS_DIR = Path(__file__).with_name("chess_piece_sets")
PIECE_SET_KEYS = (
    DEFAULT_PIECE_SET,
    "rhosgfx", "fantasy", "spatial", "celtic",
    "kiwen-suwi", "totoy", "merida", "pixel",
)

# name -> {symbol: "<g>...</g>"} for loaded sets, or None for sets whose
# file is missing/corrupt (cached too, so a broken file logs once, not once
# per render).
_piece_set_cache: dict[str, dict[str, str] | None] = {}


def _load_piece_set(name: str) -> dict[str, str] | None:
    """Vendored piece set by key, or None for the default set, an unknown
    name, or a missing/corrupt file (rendering falls back to cburnett —
    a stale stored preference must never break board rendering)."""
    if name == DEFAULT_PIECE_SET or name not in PIECE_SET_KEYS:
        return None
    if name in _piece_set_cache:
        return _piece_set_cache[name]
    try:
        with open(PIECE_SETS_DIR / f"{name}.json", encoding="utf-8") as f:
            pieces = json.load(f)
        if set(pieces) != set("PNBRQKpnbrqk"):
            raise ValueError(f"expected 12 piece symbols, got {sorted(pieces)}")
    except Exception as e:
        logging.warning(f"piece set {name!r} unavailable, using default: {e}")
        pieces = None
    _piece_set_cache[name] = pieces
    return pieces

# Board color themes. Each theme carries its square colors plus lastmove
# tints tuned to read against that palette: blue for ordinary moves, red for
# moves that captured a piece (signals 'something died here'). python-chess
# uses separate keys for light- vs dark-square tints so each palette sets
# both. Cool-toned boards (blue/slate/ice/purple) get a deeper, more
# saturated blue so the move tint doesn't melt into the squares; coffee's
# warm brown gets a hotter red for the same reason.
#
# "default" mirrors python-chess's built-in square colors and the tints the
# bot has always used, and is what every render gets today — nothing selects
# the other themes yet.
DEFAULT_BOARD_THEME = "default"

BOARD_THEMES = {
    "default": {
        "light": "#ffce9e", "dark": "#d18b47",
        "move": {"square light lastmove": "#5db0ff", "square dark lastmove": "#1e72d4"},
        "capture": {"square light lastmove": "#ff7a7a", "square dark lastmove": "#c63838"},
    },
    "brown": {  # lichess brown
        "light": "#f0d9b5", "dark": "#b58863",
        "move": {"square light lastmove": "#5db0ff", "square dark lastmove": "#1e72d4"},
        "capture": {"square light lastmove": "#ff7a7a", "square dark lastmove": "#c63838"},
    },
    "blue": {  # lichess blue
        "light": "#dee3e6", "dark": "#8ca2ad",
        "move": {"square light lastmove": "#4f9dfa", "square dark lastmove": "#1c5fc2"},
        "capture": {"square light lastmove": "#ff7a7a", "square dark lastmove": "#c23636"},
    },
    "green": {  # lichess green
        "light": "#ffffdd", "dark": "#86a666",
        "move": {"square light lastmove": "#5db0ff", "square dark lastmove": "#1e72d4"},
        "capture": {"square light lastmove": "#ff7a7a", "square dark lastmove": "#c63838"},
    },
    "purple": {
        "light": "#9f90b0", "dark": "#7d4a8d",
        "move": {"square light lastmove": "#56b8ff", "square dark lastmove": "#1f6fd6"},
        "capture": {"square light lastmove": "#ff7368", "square dark lastmove": "#c53030"},
    },
    "coffee": {
        "light": "#e8d0aa", "dark": "#9e6b4a",
        "move": {"square light lastmove": "#5db0ff", "square dark lastmove": "#1e72d4"},
        "capture": {"square light lastmove": "#ff6f5e", "square dark lastmove": "#cc2b2b"},
    },
    "ice": {
        "light": "#e8ebef", "dark": "#7d8796",
        "move": {"square light lastmove": "#4f9dfa", "square dark lastmove": "#1c5fc2"},
        "capture": {"square light lastmove": "#ff7a7a", "square dark lastmove": "#c63838"},
    },
    "charcoal": {
        "light": "#9e9e9e", "dark": "#5c5c5c",
        "move": {"square light lastmove": "#5db0ff", "square dark lastmove": "#1e72d4"},
        "capture": {"square light lastmove": "#ff7a7a", "square dark lastmove": "#c63838"},
    },
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
    theme: str = DEFAULT_BOARD_THEME,
    piece_set: str = DEFAULT_PIECE_SET,
) -> bytes:
    if cairosvg is None:
        raise RuntimeError(
            f"cairosvg unavailable ({_CAIROSVG_IMPORT_ERROR!r}); install libcairo2 in the runtime image."
        )
    check_square = board.king(board.turn) if board.is_check() else None
    # Unknown theme falls back to default rather than raising — the theme
    # will eventually come from stored per-user prefs, and a stale name
    # must never break board rendering mid-game.
    t = BOARD_THEMES.get(theme, BOARD_THEMES[DEFAULT_BOARD_THEME])
    colors = dict(t["capture"] if last_move_was_capture else t["move"])
    colors["square light"] = t["light"]
    colors["square dark"] = t["dark"]
    fill: dict[chess.Square, str] = {}
    arrows: list = []
    if threat_squares:
        for sq in threat_squares:
            fill[sq] = _THREAT_FILL_COLOR
        arrows = _threat_arrows(board, threat_squares)
    # chess.svg.board() has no piece-set parameter; it reads the module-level
    # chess.svg.PIECES dict at call time. Swap it for the duration of the
    # (fully synchronous) SVG build — no await happens inside the swap, so
    # concurrent renders can't observe the wrong set.
    pieces = _load_piece_set(piece_set)
    original_pieces = dict(chess.svg.PIECES) if pieces else None
    if pieces:
        chess.svg.PIECES.update(pieces)
    try:
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
    finally:
        if original_pieces is not None:
            chess.svg.PIECES.clear()
            chess.svg.PIECES.update(original_pieces)
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=size)
