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
