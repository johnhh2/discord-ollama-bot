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
# _inject_threat_overlays — SVG-level injection, doesn't need cairo
# -----------------------------------------------------------------------------


def test_inject_threat_overlays_adds_rect_per_square():
    """For each threat square, exactly one <rect class="threat"> is added."""
    import chess.svg
    svg = chess.svg.board(chess.Board())
    out = chess_render._inject_threat_overlays(
        svg, {chess.A1, chess.H8, chess.E4}, chess.WHITE,
    )
    # Count threat rects.
    import re
    rects = re.findall(r'<rect[^>]*class="threat"[^>]*>', out)
    assert len(rects) == 3


def test_inject_threat_overlays_includes_gradient_def():
    """When the upstream SVG didn't already define check_gradient (because
    check= wasn't passed), the helper injects one."""
    import chess.svg
    svg = chess.svg.board(chess.Board())
    assert "check_gradient" not in svg  # baseline
    out = chess_render._inject_threat_overlays(svg, {chess.A1}, chess.WHITE)
    assert "check_gradient" in out
    assert "radialGradient" in out


def test_inject_threat_overlays_reuses_existing_gradient_def():
    """When the SVG already has check_gradient (because check= was passed),
    we don't add a duplicate definition."""
    import chess.svg
    svg = chess.svg.board(chess.Board(), check=chess.E1)
    # Count opening <radialGradient tags (open+close both contain
    # "radialGradient" as a substring, so we look for the open tag).
    assert svg.count("<radialGradient") == 1
    out = chess_render._inject_threat_overlays(svg, {chess.A1}, chess.WHITE)
    # Still only one gradient def — we reused the existing one.
    assert out.count("<radialGradient") == 1


def test_inject_threat_overlays_coords_match_python_chess():
    """Our coordinate math must match python-chess's own check-square coords.
    Render a position with check on a known square; render again with no check
    but use our injector on the same square; the resulting rect coordinates
    should be identical."""
    import chess.svg
    import re
    b = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    # Reference: python-chess's own check rendering on a1.
    upstream = chess.svg.board(b, check=chess.A1, orientation=chess.WHITE)
    m_upstream = re.search(r'<rect[^>]*class="check"[^>]*x="(\d+)"[^>]*y="(\d+)"', upstream)
    if m_upstream is None:
        # Sometimes the attribute order differs.
        m_upstream = re.search(r'<rect[^>]*x="(\d+)"[^>]*y="(\d+)"[^>]*class="check"', upstream)
    assert m_upstream is not None
    upstream_x, upstream_y = m_upstream.group(1), m_upstream.group(2)

    # Our injection on the same square.
    svg = chess.svg.board(b, orientation=chess.WHITE)
    out = chess_render._inject_threat_overlays(svg, {chess.A1}, chess.WHITE)
    m_ours = re.search(r'<rect[^>]*class="threat"[^>]*>', out)
    assert m_ours is not None
    threat_rect = m_ours.group(0)
    assert f'x="{upstream_x}"' in threat_rect
    assert f'y="{upstream_y}"' in threat_rect


def test_inject_threat_overlays_flips_for_black_orientation():
    """When orientation=BLACK, the same logical square renders at a different
    pixel position (board is flipped)."""
    import chess.svg
    import re
    b = chess.Board()
    svg = chess.svg.board(b)
    white_view = chess_render._inject_threat_overlays(svg, {chess.A1}, chess.WHITE)
    black_view = chess_render._inject_threat_overlays(svg, {chess.A1}, chess.BLACK)
    white_rect = re.search(r'<rect[^>]*class="threat"[^>]*>', white_view).group(0)
    black_rect = re.search(r'<rect[^>]*class="threat"[^>]*>', black_view).group(0)
    # Different pixel coordinates for the same square.
    assert white_rect != black_rect


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
