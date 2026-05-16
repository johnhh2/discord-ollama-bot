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
