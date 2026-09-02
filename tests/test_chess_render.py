"""chess_render.render_board_png smoke tests.

Cairo is required at runtime; CI's Docker image installs libcairo2 but dev
machines (especially Windows) may not. Tests skip cleanly when cairosvg
can't load Cairo.
"""
import chess
import pytest

from src.games import chess_render


_cairo_available = chess_render.cairosvg is not None
_skip_no_cairo = pytest.mark.skipif(
    not _cairo_available,
    reason="cairosvg/libcairo not installed in this environment",
)


@_skip_no_cairo
def test_render_returns_png_bytes():
    """Default starting position renders to PNG bytes starting with the PNG magic."""
    png = chess_render.render_board_png(chess.Board())
    assert isinstance(png, bytes)
    assert len(png) > 1000  # nontrivial size
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


@_skip_no_cairo
def test_render_orientation_black():
    """Rendering with orientation=BLACK should still produce valid PNG bytes."""
    png = chess_render.render_board_png(chess.Board(), orientation=chess.BLACK)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


@_skip_no_cairo
def test_render_with_last_move_and_check():
    """A position in check renders without error (highlights the king)."""
    # Scholar's mate position: black to move, in check.
    b = chess.Board("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4")
    last_move = chess.Move.from_uci("h5f7")
    png = chess_render.render_board_png(b, last_move=last_move)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# -----------------------------------------------------------------------------
# Threat-square overlay (uses python-chess's native fill= mechanism)
# -----------------------------------------------------------------------------


@_skip_no_cairo
def test_render_with_threat_squares_produces_png():
    """Passing threat_squares={A1, H8} renders to PNG bytes without error."""
    png = chess_render.render_board_png(
        chess.Board(),
        threat_squares={chess.A1, chess.H8},
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_passes_threat_fill_to_python_chess(monkeypatch):
    """When threat_squares is set, the renderer forwards them to
    chess.svg.board() via fill={square: red}."""
    captured: dict = {}

    def _fake_svg_board(board, **kwargs):
        captured["fill"] = kwargs.get("fill")
        return "<svg/>"
    monkeypatch.setattr(chess_render.chess.svg, "board", _fake_svg_board)
    # Mock cairosvg too so the test doesn't need the native lib.
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))

    chess_render.render_board_png(
        chess.Board(),
        threat_squares={chess.A1, chess.H8},
    )

    assert captured["fill"] is not None
    assert chess.A1 in captured["fill"]
    assert chess.H8 in captured["fill"]
    # Same red applied to all threat squares.
    assert len(set(captured["fill"].values())) == 1


def test_threat_arrows_emits_attacker_and_defender(monkeypatch):
    """For a hanging piece, _threat_arrows includes one arrow per attacker
    AND one per defender (different colors)."""
    # White queen on d1 attacked by black rook on d8 (open d-file). Defended
    # by white king on e1.
    b = chess.Board("3rk3/8/8/8/8/8/8/3QK3 w - - 0 1")
    arrows = chess_render._threat_arrows(b, {chess.D1})
    # Index by (tail, head) to make assertions order-independent.
    by_pair = {(a.tail, a.head): a.color for a in arrows}
    # Black rook attacks d1.
    assert (chess.D8, chess.D1) in by_pair
    # White king defends d1.
    assert (chess.E1, chess.D1) in by_pair
    # Colors differ (red vs green).
    attacker_color = by_pair[(chess.D8, chess.D1)]
    defender_color = by_pair[(chess.E1, chess.D1)]
    assert attacker_color != defender_color


def test_threat_arrows_color_by_side():
    """White attackers use the white-shade red; black attackers use the
    black-shade red. Same for defenders."""
    # White knight on c4 attacks black knight on e5. Black king on d6 defends.
    b = chess.Board("8/8/3k4/4n3/2N5/8/8/4K3 w - - 0 1")
    arrows = chess_render._threat_arrows(b, {chess.E5})
    by_pair = {(a.tail, a.head): a.color for a in arrows}
    # White knight attacking: white-shade red.
    assert by_pair[(chess.C4, chess.E5)] == chess_render._ATTACKER_ARROW_COLOR[chess.WHITE]
    # Black king defending: black-shade green.
    assert by_pair[(chess.D6, chess.E5)] == chess_render._DEFENDER_ARROW_COLOR[chess.BLACK]


def test_threat_arrows_multiple_attackers():
    """When a piece has multiple attackers, each gets its own arrow."""
    # Black knight on e5 attacked by white bishop b2 and white knight c4.
    b = chess.Board("4k3/8/8/4n3/2N5/8/1B6/4K3 w - - 0 1")
    arrows = chess_render._threat_arrows(b, {chess.E5})
    attacker_tails = {a.tail for a in arrows if a.head == chess.E5
                      and a.color == chess_render._ATTACKER_ARROW_COLOR[chess.WHITE]}
    assert chess.B2 in attacker_tails
    assert chess.C4 in attacker_tails


def test_threat_arrows_empty_when_no_threats():
    """No threat squares → no arrows."""
    b = chess.Board()
    arrows = chess_render._threat_arrows(b, set())
    assert arrows == []


def test_render_passes_threat_arrows_to_python_chess(monkeypatch):
    """When threat_squares is set, the renderer forwards arrows to
    chess.svg.board() via arrows=."""
    captured: dict = {}

    def _fake_svg_board(board, **kwargs):
        captured["arrows"] = kwargs.get("arrows")
        return "<svg/>"
    monkeypatch.setattr(chess_render.chess.svg, "board", _fake_svg_board)
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))

    # Position with at least one hanging piece (white queen on d1).
    b = chess.Board("3rk3/8/8/8/8/8/8/3QK3 w - - 0 1")
    chess_render.render_board_png(b, threat_squares={chess.D1})

    assert captured["arrows"]  # non-empty list of arrows
    assert len(captured["arrows"]) >= 1


def test_render_no_threat_squares_passes_empty_fill(monkeypatch):
    """When threat_squares is omitted, fill= is empty (no overlays)."""
    captured: dict = {}

    def _fake_svg_board(board, **kwargs):
        captured["fill"] = kwargs.get("fill")
        return "<svg/>"
    monkeypatch.setattr(chess_render.chess.svg, "board", _fake_svg_board)
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))

    chess_render.render_board_png(chess.Board())

    assert captured["fill"] == {}


# -----------------------------------------------------------------------------
# Board themes
# -----------------------------------------------------------------------------


def test_all_themes_have_complete_palettes():
    """Every theme defines square colors plus full move/capture tint pairs,
    and the default theme name resolves."""
    assert chess_render.DEFAULT_BOARD_THEME in chess_render.BOARD_THEMES
    for name, t in chess_render.BOARD_THEMES.items():
        assert set(t) == {"light", "dark", "move", "capture"}, name
        for palette in ("move", "capture"):
            assert set(t[palette]) == {
                "square light lastmove", "square dark lastmove",
            }, (name, palette)


def test_default_theme_matches_python_chess_square_colors():
    """The default theme's square colors mirror python-chess's built-ins, so
    adding the theme hook changed nothing about today's renders."""
    t = chess_render.BOARD_THEMES["default"]
    assert t["light"] == "#ffce9e"
    assert t["dark"] == "#d18b47"


def _capture_colors(monkeypatch):
    captured: dict = {}

    def _fake_svg_board(board, **kwargs):
        captured.update(kwargs.get("colors") or {})
        return "<svg/>"
    monkeypatch.setattr(chess_render.chess.svg, "board", _fake_svg_board)
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))
    return captured


def test_render_theme_sets_square_and_move_colors(monkeypatch):
    """theme= drives both the square colors and the (non-capture) lastmove
    tint passed to chess.svg.board()."""
    captured = _capture_colors(monkeypatch)
    chess_render.render_board_png(chess.Board(), theme="blue")
    t = chess_render.BOARD_THEMES["blue"]
    assert captured["square light"] == t["light"]
    assert captured["square dark"] == t["dark"]
    assert captured["square light lastmove"] == t["move"]["square light lastmove"]
    assert captured["square dark lastmove"] == t["move"]["square dark lastmove"]


def test_render_theme_capture_uses_red_palette(monkeypatch):
    """last_move_was_capture=True selects the theme's capture (red) tints."""
    captured = _capture_colors(monkeypatch)
    chess_render.render_board_png(
        chess.Board(), theme="coffee", last_move_was_capture=True,
    )
    t = chess_render.BOARD_THEMES["coffee"]
    assert captured["square light lastmove"] == t["capture"]["square light lastmove"]
    assert captured["square dark lastmove"] == t["capture"]["square dark lastmove"]


def test_render_unknown_theme_falls_back_to_default(monkeypatch):
    """A stale/unknown theme name renders with the default palette instead of
    raising — board rendering must never break mid-game."""
    captured = _capture_colors(monkeypatch)
    chess_render.render_board_png(chess.Board(), theme="no-such-theme")
    t = chess_render.BOARD_THEMES[chess_render.DEFAULT_BOARD_THEME]
    assert captured["square light"] == t["light"]
    assert captured["square dark"] == t["dark"]


# -----------------------------------------------------------------------------
# Piece sets
# -----------------------------------------------------------------------------


def test_all_vendored_piece_sets_load_and_parse():
    """Every non-default set loads 12 symbols, each a well-formed <g> with
    the id python-chess's <use href="#{color}-{piece}"> lookup expects."""
    import xml.etree.ElementTree as ET

    expected_ids = {
        "P": "white-pawn", "N": "white-knight", "B": "white-bishop",
        "R": "white-rook", "Q": "white-queen", "K": "white-king",
        "p": "black-pawn", "n": "black-knight", "b": "black-bishop",
        "r": "black-rook", "q": "black-queen", "k": "black-king",
    }
    for name in chess_render.PIECE_SET_KEYS:
        if name == chess_render.DEFAULT_PIECE_SET:
            continue
        pieces = chess_render._load_piece_set(name)
        assert pieces is not None, name
        assert set(pieces) == set(expected_ids), name
        for sym, raw in pieces.items():
            el = ET.fromstring(raw)
            assert el.get("id") == expected_ids[sym], (name, sym)


def test_render_piece_set_swaps_and_restores(monkeypatch):
    """piece_set= swaps chess.svg.PIECES for the duration of the svg build
    and restores the built-in set afterwards, even though the swap has no
    official python-chess API."""
    original_pawn = chess_render.chess.svg.PIECES["P"]
    seen: dict = {}

    def _fake_svg_board(board, **kwargs):
        seen["pawn"] = chess_render.chess.svg.PIECES["P"]
        return "<svg/>"
    monkeypatch.setattr(chess_render.chess.svg, "board", _fake_svg_board)
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))

    chess_render.render_board_png(chess.Board(), piece_set="rhosgfx")

    assert seen["pawn"] != original_pawn
    assert chess_render.chess.svg.PIECES["P"] == original_pawn


def test_render_piece_set_restores_when_board_raises(monkeypatch):
    """A failure inside chess.svg.board must not leave the swapped piece set
    behind for every later render."""
    original_pawn = chess_render.chess.svg.PIECES["P"]

    def _boom(board, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(chess_render.chess.svg, "board", _boom)
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))

    with pytest.raises(RuntimeError, match="boom"):
        chess_render.render_board_png(chess.Board(), piece_set="rhosgfx")

    assert chess_render.chess.svg.PIECES["P"] == original_pawn


def test_render_unknown_piece_set_uses_default(monkeypatch):
    """A stale/unknown piece-set name renders with the built-in set instead
    of raising — rendering must never break mid-game."""
    original_pawn = chess_render.chess.svg.PIECES["P"]
    seen: dict = {}

    def _fake_svg_board(board, **kwargs):
        seen["pawn"] = chess_render.chess.svg.PIECES["P"]
        return "<svg/>"
    monkeypatch.setattr(chess_render.chess.svg, "board", _fake_svg_board)
    monkeypatch.setattr(chess_render, "cairosvg",
                        type("X", (), {"svg2png": staticmethod(lambda **k: b"PNG")}))

    chess_render.render_board_png(chess.Board(), piece_set="no-such-set")

    assert seen["pawn"] == original_pawn


@_skip_no_cairo
def test_render_vendored_set_produces_png():
    """End-to-end: a vendored set renders to real PNG bytes."""
    png = chess_render.render_board_png(chess.Board(), piece_set="rhosgfx")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_fails_loudly_without_cairo(monkeypatch):
    """When cairosvg is unavailable, render_board_png raises RuntimeError with
    a helpful message — not a cryptic OSError from deep inside cairosvg."""
    monkeypatch.setattr(chess_render, "cairosvg", None)
    monkeypatch.setattr(
        chess_render, "_CAIROSVG_IMPORT_ERROR",
        ImportError("simulated missing libcairo"),
    )
    with pytest.raises(RuntimeError, match="cairosvg unavailable"):
        chess_render.render_board_png(chess.Board())
