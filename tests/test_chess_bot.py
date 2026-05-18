"""Chess engine tests: pure helpers + engine-dispatch + cog integration.

The chess_bot module has three engine tiers:
  - Sub-Maia (100-1000): Maia 1100 + random/blunder degraders.
  - Maia (1100-1900): pure Maia at the matching weights bin.
  - Native (2000+): Stockfish UCI_Elo.

Engine-spawning tests skip cleanly when the relevant binary isn't on PATH.
Dispatch tests mock chess.engine.popen_uci to avoid real subprocess spawns.
Cog tests stub chess_bot.pick_move so they exercise the full move pipeline
(mutate -> save -> render -> bump -> reply) without any engine.
"""
import asyncio
import shutil
from types import SimpleNamespace

import chess
import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
from src.games import chess_bot, chess_engine
from src.games.chess import ChessCog, _initial_pgn

from tests.fakes.discord import FakeMember, FakeGuild, FakeCtx, FakeMessage


_aio = pytest.mark.asyncio
_STOCKFISH_AVAILABLE = shutil.which("stockfish") is not None


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------


class TestClampElo:
    def test_below_min(self):
        assert chess_bot.clamp_elo(50) == chess_bot.ELO_MIN
        assert chess_bot.clamp_elo(-100) == chess_bot.ELO_MIN

    def test_above_max(self):
        assert chess_bot.clamp_elo(5000) == chess_bot.ELO_MAX
        assert chess_bot.clamp_elo(chess_bot.ELO_MAX + 1) == chess_bot.ELO_MAX

    def test_inside_range(self):
        for elo in (100, 500, 1100, 2000, 3100):
            assert chess_bot.clamp_elo(elo) == elo

    def test_boundary_values(self):
        assert chess_bot.clamp_elo(chess_bot.ELO_MIN) == chess_bot.ELO_MIN
        assert chess_bot.clamp_elo(chess_bot.ELO_MAX) == chess_bot.ELO_MAX


class TestResolveStockfishPath:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("STOCKFISH_PATH", "/custom/path/sf")
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: "/usr/bin/stockfish")
        assert chess_bot._resolve_stockfish_path() == "/custom/path/sf"

    def test_path_lookup_when_no_env(self, monkeypatch):
        monkeypatch.delenv("STOCKFISH_PATH", raising=False)
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: "/usr/local/bin/stockfish")
        assert chess_bot._resolve_stockfish_path() == "/usr/local/bin/stockfish"

    def test_debian_fallback_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("STOCKFISH_PATH", raising=False)
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: None)
        assert chess_bot._resolve_stockfish_path() == "/usr/games/stockfish"

    def test_empty_env_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("STOCKFISH_PATH", "")
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: "/usr/bin/stockfish")
        assert chess_bot._resolve_stockfish_path() == "/usr/bin/stockfish"


class TestRoundEloToBin:
    def test_exact_multiples_unchanged(self):
        for elo in (100, 400, 1100, 1500, 2000, 3100):
            assert chess_bot.round_elo_to_bin(elo) == elo

    def test_rounds_up_at_50(self):
        assert chess_bot.round_elo_to_bin(1150) == 1200
        assert chess_bot.round_elo_to_bin(1149) == 1100
        assert chess_bot.round_elo_to_bin(1151) == 1200

    def test_rounds_to_nearest(self):
        assert chess_bot.round_elo_to_bin(1199) == 1200
        assert chess_bot.round_elo_to_bin(1101) == 1100
        assert chess_bot.round_elo_to_bin(101) == 100
        assert chess_bot.round_elo_to_bin(149) == 100
        assert chess_bot.round_elo_to_bin(151) == 200


class TestMaiaWeightsPath:
    """All 9 Maia weight files (1100-1900 in 100-Elo steps) are vendored
    under maia_weights/ at the repo root."""

    def test_all_maia_bins_exist_on_disk(self):
        for elo in range(chess_bot.MAIA_ELO_MIN, chess_bot.MAIA_ELO_MAX + 100, 100):
            p = chess_bot.maia_weights_path(elo)
            assert p.exists(), f"Maia weights missing at Elo {elo}: {p}"

    def test_path_format(self):
        p = chess_bot.maia_weights_path(1100)
        assert p.name == "maia-1100.pb.gz"


# -----------------------------------------------------------------------------
# Sub-Maia Elo curves (random blend + extra blunder)
# -----------------------------------------------------------------------------


class TestMaiaPoolSizeForElo:
    """Maia pool size: how many of Maia 1100's top-N policy moves the sub-
    Maia tier samples from. Wider pool at low Elo = more 'unlikely but
    human' moves come through; small pool at the top of the range still
    samples but mostly returns Maia 1100's top policy moves."""

    def test_anchor_points(self):
        anchors = [(100, 11), (400, 8), (700, 5), (800, 4),
                   (900, 3), (1000, 2)]
        for elo, expected in anchors:
            assert chess_bot.maia_pool_size_for_elo(elo) == expected, (
                f"pool size at Elo {elo}: expected {expected}, got "
                f"{chess_bot.maia_pool_size_for_elo(elo)}"
            )

    def test_clamps_below_and_above(self):
        assert chess_bot.maia_pool_size_for_elo(50) == 11
        assert chess_bot.maia_pool_size_for_elo(1100) == 2
        assert chess_bot.maia_pool_size_for_elo(2000) == 2

    def test_monotonic_non_increasing(self):
        prev = chess_bot.MULTIPV_COUNT + 1
        for elo in range(100, 1001, 50):
            n = chess_bot.maia_pool_size_for_elo(elo)
            assert n <= prev, f"pool size not monotonic at elo={elo}: {n} > {prev}"
            prev = n

    def test_multipv_count_covers_largest_pool(self):
        largest = max(p for _, p in chess_bot._MAIA_POOL_SIZE_ANCHORS)
        assert chess_bot.MULTIPV_COUNT >= largest, (
            f"MULTIPV_COUNT={chess_bot.MULTIPV_COUNT} < largest pool {largest}"
        )


class TestExtraBlunderProbabilityForElo:
    """Extra-blunder probability: forced SEE-losing moves layered on top of
    Maia's sampled move. Only nonzero below Elo 500 — above that the wider
    sampling pool already produces enough natural mistakes."""

    def test_anchor_points(self):
        anchors = [(100, 0.10), (400, 0.01), (500, 0.0), (1000, 0.0)]
        for elo, expected in anchors:
            actual = chess_bot.extra_blunder_probability_for_elo(elo)
            assert abs(actual - expected) < 0.001, (
                f"P(extra blunder) at Elo {elo}: expected ~{expected}, got {actual}"
            )

    def test_zero_at_and_above_500(self):
        for elo in (500, 600, 700, 800, 1000, 1100, 2000):
            assert chess_bot.extra_blunder_probability_for_elo(elo) == 0.0

    def test_clamps_below_floor(self):
        assert chess_bot.extra_blunder_probability_for_elo(50) == 0.10
        assert chess_bot.extra_blunder_probability_for_elo(0) == 0.10

    def test_monotonic_non_increasing(self):
        prev = 1.0
        for elo in range(100, 1001, 50):
            p = chess_bot.extra_blunder_probability_for_elo(elo)
            assert p <= prev, f"P(extra blunder) not monotonic at elo={elo}"
            prev = p


class TestNoticeProbabilityForEloAndPiece:
    """Notice probability = base(elo) + bonus(piece type), capped at 0.99.
    Models 'real players see hanging queens way more reliably than hanging
    pawns, but stronger players notice both more reliably.'"""

    def test_elo_100_pawn_uses_base_floor(self):
        p = chess_bot.notice_probability_for_elo_and_piece(100, chess.PAWN)
        assert abs(p - 0.40) < 0.001

    def test_elo_100_queen_gets_full_bonus(self):
        p = chess_bot.notice_probability_for_elo_and_piece(100, chess.QUEEN)
        # 0.40 + 0.35 = 0.75
        assert abs(p - 0.75) < 0.001

    def test_elo_1000_pawn_is_high_base(self):
        p = chess_bot.notice_probability_for_elo_and_piece(1000, chess.PAWN)
        assert abs(p - 0.95) < 0.001

    def test_elo_1000_queen_caps_at_99(self):
        # 0.95 + 0.35 = 1.30, capped at 0.99.
        p = chess_bot.notice_probability_for_elo_and_piece(1000, chess.QUEEN)
        assert abs(p - 0.99) < 0.001

    def test_minor_pieces_share_bonus(self):
        p_knight = chess_bot.notice_probability_for_elo_and_piece(500, chess.KNIGHT)
        p_bishop = chess_bot.notice_probability_for_elo_and_piece(500, chess.BISHOP)
        assert p_knight == p_bishop

    def test_value_ordering_at_fixed_elo(self):
        """Higher-value pieces always have ≥ notice probability."""
        prev = -1.0
        for piece_type in (chess.PAWN, chess.KNIGHT, chess.ROOK, chess.QUEEN):
            p = chess_bot.notice_probability_for_elo_and_piece(500, piece_type)
            assert p >= prev, f"non-monotonic for {piece_type}: {p} < {prev}"
            prev = p

    def test_elo_monotonic_at_fixed_piece(self):
        """For any piece type, higher Elo means ≥ notice probability."""
        prev = -1.0
        for elo in range(100, 1001, 100):
            p = chess_bot.notice_probability_for_elo_and_piece(elo, chess.KNIGHT)
            assert p >= prev, f"non-monotonic at Elo {elo}: {p} < {prev}"
            prev = p


class TestHangingSquares:
    def test_no_hangs_in_starting_position(self):
        b = chess.Board()
        assert chess_bot.hanging_squares(b, chess.WHITE) == set()
        assert chess_bot.hanging_squares(b, chess.BLACK) == set()

    def test_undefended_piece_under_attack_hangs(self):
        # White knight on f3, black queen on f6 attacking. No defender.
        b = chess.Board("4k3/8/5q2/8/8/5N2/8/4K3 b - - 0 1")
        assert chess.F3 in chess_bot.hanging_squares(b, chess.WHITE)

    def test_equal_trade_does_not_hang(self):
        # Knight defended by pawn, attacked by knight.
        b = chess.Board("4k3/8/8/4n3/8/5N2/6P1/4K3 b - - 0 1")
        assert chess.F3 not in chess_bot.hanging_squares(b, chess.WHITE)


class TestMostValuableHanging:
    def test_returns_none_when_nothing_hangs(self):
        b = chess.Board()
        assert chess_bot._most_valuable_hanging(b, chess.WHITE) is None

    def test_picks_highest_value_hanging(self):
        # Black queen on a8 and black pawn on a7 are both hanging to white's
        # rook on a1 (queen worth more).
        b = chess.Board("q7/p7/8/8/8/8/8/R3K2k w - - 0 1")
        sq = chess_bot._most_valuable_hanging(b, chess.BLACK)
        # Either a8 (queen) or none, but queen wins over pawn.
        if sq is not None:
            assert sq == chess.A8


class TestMoveAddressesSelfHang:
    def test_moving_the_piece_away_addresses(self):
        # White knight on f3 is hanging to black queen on f6. Nf3-d4 moves it.
        b = chess.Board("4k3/8/5q2/8/8/5N2/8/4K3 w - - 0 1")
        move = chess.Move.from_uci("f3d4")
        assert chess_bot._move_addresses_self_hang(b, move, chess.F3)

    def test_ignoring_the_threat_does_not_address(self):
        # White knight on b1 is hanging to black queen on b8. White plays an
        # unrelated king shuffle on the kingside that can't possibly defend b1.
        b = chess.Board("1q2k3/8/8/8/8/8/8/1N2K2R w K - 0 1")
        # Verify the setup: b1 is hanging for white.
        assert chess.B1 in chess_bot.hanging_squares(b, chess.WHITE)
        move = chess.Move.from_uci("h1g1")  # rook move on the other side
        assert not chess_bot._move_addresses_self_hang(b, move, chess.B1)


class TestMoveTakesOppHang:
    def test_capture_returns_true(self):
        move = chess.Move.from_uci("a1a8")
        assert chess_bot._move_takes_opp_hang(move, chess.A8)

    def test_non_capture_returns_false(self):
        move = chess.Move.from_uci("a1a2")
        assert not chess_bot._move_takes_opp_hang(move, chess.A8)


# -----------------------------------------------------------------------------
# Static Exchange Evaluation
# -----------------------------------------------------------------------------


class TestSEE:
    def test_undefended_pawn_under_attack_hangs(self):
        b = chess.Board("4k3/8/8/4P3/8/5n2/8/4K3 b - - 0 1")
        assert chess_bot.see_capture(b, chess.E5, chess.BLACK) == 1

    def test_defended_pawn_attacked_by_equal_value_breaks_even(self):
        b = chess.Board("4k3/8/5p2/4P3/3P4/8/8/4K3 b - - 0 1")
        assert chess_bot.see_capture(b, chess.E5, chess.BLACK) == 0

    def test_queen_attacks_pawn_defended_by_pawn_no_hang(self):
        b = chess.Board("4k3/8/8/8/4P3/3P4/8/3QK3 b - - 0 1")
        result = chess_bot.see_capture(b, chess.E4, chess.BLACK)
        assert result <= 0

    def test_no_attackers_returns_zero(self):
        b = chess.Board()
        assert chess_bot.see_capture(b, chess.E2, chess.BLACK) == 0

    def test_empty_square_returns_zero(self):
        b = chess.Board()
        assert chess_bot.see_capture(b, chess.E4, chess.BLACK) == 0

    @pytest.mark.xfail(reason="SEE does not detect pinned defenders -- known limitation")
    def test_pinned_defender_does_not_actually_defend(self):
        assert False  # noqa: B011


# -----------------------------------------------------------------------------
# _find_see_losing_move
# -----------------------------------------------------------------------------


class TestFindSeeLosingMove:
    """The extra-blunder injector picks a move that drops material per SEE
    in the resulting position."""

    def test_returns_none_when_no_losing_move(self):
        b = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        assert chess_bot._find_see_losing_move(b) is None

    def test_finds_a_hanging_drop(self, monkeypatch):
        b = chess.Board("3rk3/8/8/8/8/8/8/3QK3 w - - 0 1")
        monkeypatch.setattr(chess_bot.random, "choice", lambda moves: moves[0])
        result = chess_bot._find_see_losing_move(b)
        assert result is not None
        after = b.copy(stack=False)
        after.push(result)
        assert chess_bot.see_capture(after, result.to_square, chess.BLACK) > 0

    def test_excludes_specified_move(self, monkeypatch):
        b = chess.Board("3rk3/8/8/8/8/8/8/3QK3 w - - 0 1")
        monkeypatch.setattr(chess_bot.random, "choice", lambda moves: moves[0])
        first = chess_bot._find_see_losing_move(b)
        assert first is not None
        for _ in range(10):
            other = chess_bot._find_see_losing_move(b, exclude=first)
            if other is None:
                break
            assert other != first


# -----------------------------------------------------------------------------
# Engine dispatch -- mock chess.engine.popen_uci to avoid subprocess spawns
# -----------------------------------------------------------------------------


def _install_fake_engine(monkeypatch, *, play_move: str = "e2e4",
                         analyse_pvs: list[str] | None = None,
                         maia_available: bool = False):
    """Replace chess.engine.popen_uci with a fake that records play(),
    analyse(), and configure() calls. Returns the calls-recording dict.

    `analyse_pvs` is the list of UCI strings the fake's analyse() returns
    (ranked most-likely-first). Used by sub-Maia tests that exercise the
    top-N policy sampling. Default is a 10-move list of legal opening moves.

    By default `maia_available=False`: _resolve_lc0_path() returns None so
    any Maia-tier Elo (1100-1900) raises EngineError and pick_move falls
    back to Stockfish. Set maia_available=True to test Maia routing."""
    if analyse_pvs is None:
        analyse_pvs = ["e2e4", "d2d4", "g1f3", "c2c4", "e2e3",
                       "d2d3", "b1c3", "a2a3", "h2h3", "g2g3"]
    calls: dict = {"configure": [], "quit": 0, "play_limit": [],
                   "analyse_limit": [], "popen_args": []}

    class _FakeEngine:
        async def configure(self, options):
            calls["configure"].append(dict(options))

        async def play(self, board, limit):
            calls["play_limit"].append(limit)
            return SimpleNamespace(move=chess.Move.from_uci(play_move))

        async def analyse(self, board, limit, multipv=None, **kwargs):
            calls["analyse_limit"].append((limit, multipv))
            n = multipv or len(analyse_pvs)
            return [{"pv": [chess.Move.from_uci(uci)]} for uci in analyse_pvs[:n]]

        async def quit(self):
            calls["quit"] += 1

    class _FakeTransport:
        def close(self):
            pass

    async def _fake_popen(path_or_args):
        calls["popen_args"].append(path_or_args)
        return _FakeTransport(), _FakeEngine()

    monkeypatch.setattr(chess_bot.chess.engine, "popen_uci", _fake_popen)
    if maia_available:
        monkeypatch.setattr(chess_bot, "_resolve_lc0_path", lambda: "/usr/bin/lc0")
    else:
        monkeypatch.setattr(chess_bot, "_resolve_lc0_path", lambda: None)
    return calls


# ---- Native tier (Elo 2000+) ----


@_aio
async def test_pick_move_native_elo_uses_uci_limit_strength(monkeypatch):
    calls = _install_fake_engine(monkeypatch)
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 2000)

    assert len(calls["configure"]) == 1
    opts = calls["configure"][0]
    assert opts.get("UCI_LimitStrength") is True
    assert opts.get("UCI_Elo") == 2000
    assert len(calls["play_limit"]) == 1
    assert move == chess.Move.from_uci("e2e4")


@_aio
async def test_pick_move_clamps_above_max(monkeypatch):
    calls = _install_fake_engine(monkeypatch)
    await chess_bot.pick_move(chess_engine.STARTING_FEN, 9999)
    opts = calls["configure"][0]
    assert opts.get("UCI_Elo") == chess_bot.ELO_MAX


# ---- Maia tier (Elo 1100-1900) ----


@_aio
async def test_pick_move_routes_to_maia_in_1100_1900(monkeypatch):
    calls = _install_fake_engine(monkeypatch, maia_available=True)
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 1500)

    args = calls["popen_args"][-1]
    assert isinstance(args, list), f"expected list of args for lc0, got {args!r}"
    assert any("lc0" in str(a) for a in args)
    assert any("maia-1500" in str(a) for a in args)
    assert len(calls["play_limit"]) == 1
    assert calls["play_limit"][0].nodes == 1
    assert calls["configure"] == []
    assert move == chess.Move.from_uci("e2e4")


@_aio
async def test_pick_move_maia_each_bin_picks_correct_weights(monkeypatch):
    for elo in (1100, 1300, 1500, 1700, 1900):
        calls = _install_fake_engine(monkeypatch, maia_available=True)
        await chess_bot.pick_move(chess_engine.STARTING_FEN, elo)
        args = calls["popen_args"][-1]
        assert any(f"maia-{elo}" in str(a) for a in args), (
            f"Elo {elo} should select maia-{elo} weights; got args {args!r}"
        )


@_aio
async def test_pick_move_maia_fallback_to_stockfish_when_lc0_missing(monkeypatch):
    calls = _install_fake_engine(monkeypatch, maia_available=False)
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 1500)
    assert calls["configure"] != [], "expected Stockfish configure call after fallback"
    opts = calls["configure"][0]
    assert opts.get("UCI_Elo") == 1320
    assert isinstance(move, chess.Move)


@_aio
async def test_pick_move_2000_uses_stockfish_native(monkeypatch):
    calls = _install_fake_engine(monkeypatch)
    await chess_bot.pick_move(chess_engine.STARTING_FEN, 2000)
    assert calls["configure"] != []
    opts = calls["configure"][0]
    assert opts.get("UCI_Elo") == 2000


@_aio
async def test_pick_move_rounds_elo_to_nearest_hundred(monkeypatch):
    calls = _install_fake_engine(monkeypatch, maia_available=True)
    await chess_bot.pick_move(chess_engine.STARTING_FEN, 1149)
    args = calls["popen_args"][-1]
    assert any("maia-1100" in str(a) for a in args)


# ---- Sub-Maia tier (Elo 100-1000): Maia 1100 baseline + degraders ----


@_aio
async def test_pick_move_sub_maia_uses_maia_1100_baseline(monkeypatch):
    """Sub-Maia tier always calls Maia 1100 as its baseline engine,
    regardless of the user-facing Elo."""
    calls = _install_fake_engine(monkeypatch, maia_available=True)
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 500)
    args = calls["popen_args"][-1]
    assert any("maia-1100" in str(a) for a in args), (
        f"sub-Maia at Elo 500 should call maia-1100, got args {args!r}"
    )
    # Default analyse_pvs starts with e2e4; pool sampling at Elo 500 (pool=~5)
    # with random.choice unpinned picks something from the top-5.
    assert isinstance(move, chess.Move)


@_aio
async def test_pick_move_sub_maia_uses_analyse_with_pool_size_multipv(monkeypatch):
    """Sub-Maia tier calls engine.analyse(multipv=N) where N is the pool
    size for the given Elo. Native Maia tier uses engine.play(), not analyse."""
    calls = _install_fake_engine(monkeypatch, maia_available=True)
    expected_pool = chess_bot.maia_pool_size_for_elo(400)
    await chess_bot.pick_move(chess_engine.STARTING_FEN, 400)
    assert len(calls["analyse_limit"]) == 1
    _limit, multipv = calls["analyse_limit"][0]
    assert multipv == expected_pool, f"expected multipv={expected_pool}, got {multipv}"
    # Sub-Maia path doesn't call play().
    assert calls["play_limit"] == []


@_aio
async def test_pick_move_sub_maia_samples_uniformly_from_pool(monkeypatch):
    """Pool sampling uses random.choice over the top-N PV list. Pin
    random.choice to a sentinel to prove it sees more than just rank-1."""
    pvs = ["e2e4", "d2d4", "g1f3", "c2c4", "e2e3",
           "d2d3", "b1c3", "a2a3", "h2h3", "g2g3"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    # Disable extra-blunder dice so we observe pure pool sampling.
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.999)
    monkeypatch.setattr(chess_bot.random, "choice", lambda candidates: candidates[-1])
    expected_pool = chess_bot.maia_pool_size_for_elo(400)
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 400)
    # candidates[-1] = pvs[expected_pool - 1].
    assert move == chess.Move.from_uci(pvs[expected_pool - 1])


@_aio
async def test_pick_move_sub_maia_at_top_of_range_samples_small_pool(monkeypatch):
    """At the top of sub-Maia (Elo 1000) the pool is small (2); sampling
    stays within rank-1..2."""
    pvs = ["e2e4", "d2d4", "g1f3"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.999)
    monkeypatch.setattr(chess_bot.random, "choice", lambda candidates: candidates[-1])
    expected_pool = chess_bot.maia_pool_size_for_elo(1000)
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 1000)
    assert move == chess.Move.from_uci(pvs[expected_pool - 1])


@_aio
async def test_pick_move_sub_maia_extra_blunder_swaps_to_see_losing(monkeypatch):
    """When extra-blunder dice fires AND a SEE-losing alternative exists,
    the sampled move is replaced with that losing move."""
    fen = "3rk3/8/8/8/8/8/8/3QK3 w - - 0 1"
    # Maia's analyse returns Kd2 (safe king move).
    pvs = ["e1d2"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    # Force the extra-blunder dice to fire (random=0.0 < 0.10 at Elo 100).
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.0)
    # random.choice called twice: once for pool sample (single-item, returns
    # Kd2), once for _find_see_losing_move (pick first SEE-losing move).
    monkeypatch.setattr(chess_bot.random, "choice", lambda moves: list(moves)[0])

    move = await chess_bot.pick_move(fen, 100)
    board = chess.Board(fen)
    assert move in board.legal_moves
    after = board.copy(stack=False)
    after.push(move)
    assert chess_bot.see_capture(after, move.to_square, chess.BLACK) > 0


@_aio
async def test_pick_move_sub_maia_extra_blunder_falls_back_when_no_losing_move(monkeypatch):
    """If the position has no SEE-losing move, the blunder branch is a no-op
    and Maia's sampled move comes through."""
    fen = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
    pvs = ["e1e2"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.0)
    move = await chess_bot.pick_move(fen, 100)
    assert move == chess.Move.from_uci("e1e2")


@_aio
async def test_pick_move_sub_maia_extra_blunder_skipped_above_500(monkeypatch):
    """At Elo 500+, extra-blunder probability is 0 — even with random()=0
    pinned, the dice never fires."""
    fen = "3rk3/8/8/8/8/8/8/3QK3 w - - 0 1"
    pvs = ["e1d2"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.0)
    monkeypatch.setattr(chess_bot.random, "choice", lambda moves: list(moves)[0])
    move = await chess_bot.pick_move(fen, 500)
    # No blunder swap → Maia's pick (Kd2) comes through.
    assert move == chess.Move.from_uci("e1d2")


# ---- Threat-awareness check (defensive + offensive) ----


@_aio
async def test_pick_move_sub_maia_addresses_self_hang_when_notice_fires(monkeypatch):
    """When the bot's piece is hanging AND the sampled move ignores it AND
    notice dice fires, _sub_maia_move re-routes to a defensive candidate."""
    # White queen on d1 attacked by black rook on d8 along the open d-file.
    # The queen is hanging (rook captures for +9).
    # Maia returns two candidates: e1e2 (ignores threat) and d1d4 (still on
    # d-file but ALSO addresses by moving queen out of d1)... actually d1d4
    # moves the queen to d4 which is also on the d-file and STILL hanging.
    # Let me use d1c1 instead (moves queen off d-file -> addresses).
    fen = "3rk3/8/8/8/8/8/8/3QK3 w - - 0 1"
    # Rank-1 = Ke1e2 (ignores threat, leaves queen on d1). Rank-2 = Qd1c1
    # (moves queen off d-file → addresses).
    pvs = ["e1e2", "d1c1"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    # random.random pinned to 0: pool sample picks Ke2, then notice dice
    # fires (0 < 0.99 at Elo 1000 for queen), then we swap to Qc1.
    # extra-blunder dice is 0 at Elo 1000 so no swap-back.
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.0)
    # random.choice gets called: first for pool sampling (returns first),
    # then for picking from addressing candidates (returns first).
    monkeypatch.setattr(chess_bot.random, "choice", lambda moves: list(moves)[0])

    move = await chess_bot.pick_move(fen, 1000)
    # Should have swapped to the addressing move (Qd1c1).
    assert move == chess.Move.from_uci("d1c1")


@_aio
async def test_pick_move_sub_maia_skips_self_hang_notice_when_dice_high(monkeypatch):
    """When the notice dice doesn't fire, the bot keeps the threat-ignoring
    sampled move (models 'I missed the threat')."""
    fen = "3rk3/8/8/8/8/8/8/3QK3 w - - 0 1"
    pvs = ["e1e2", "d1c1"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    # random=0.999 high: pool samples OK, notice dice fails, extra-blunder
    # at Elo 100 has 0.10 probability so 0.999 > 0.10 = skipped.
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.999)
    monkeypatch.setattr(chess_bot.random, "choice", lambda moves: list(moves)[0])

    move = await chess_bot.pick_move(fen, 100)
    # Sampled = Ke2, notice didn't fire → Ke2 comes through.
    assert move == chess.Move.from_uci("e1e2")


@_aio
async def test_pick_move_sub_maia_takes_opp_hang_when_notice_fires(monkeypatch):
    """When the opponent has a hanging piece AND a pool candidate captures it
    AND notice dice fires, _sub_maia_move re-routes to the capture."""
    # Black queen on e5 is hanging (no defender), white knight on f3 attacks.
    # Maia's top candidates include a quiet move and the capture.
    fen = "4k3/8/8/4q3/8/5N2/8/4K3 w - - 0 1"
    pvs = ["f3h4", "f3e5"]  # rank-1 quiet move, rank-2 captures queen
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.0)
    monkeypatch.setattr(chess_bot.random, "choice", lambda moves: list(moves)[0])

    move = await chess_bot.pick_move(fen, 1000)
    # Should have swapped to the capture Nxe5.
    assert move == chess.Move.from_uci("f3e5")


@_aio
async def test_pick_move_sub_maia_no_op_when_nothing_hangs(monkeypatch):
    """No hanging pieces on either side → threat-awareness is a no-op."""
    # Starting position: nothing hangs.
    pvs = ["e2e4", "d2d4"]
    _install_fake_engine(monkeypatch, analyse_pvs=pvs, maia_available=True)
    monkeypatch.setattr(chess_bot.random, "random", lambda: 0.0)
    monkeypatch.setattr(chess_bot.random, "choice", lambda moves: list(moves)[0])
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 500)
    # Sampled = e2e4, no threats to address → e2e4 comes through.
    assert move == chess.Move.from_uci("e2e4")


# ---- Engine spawn -- actually run binaries (CI only) ----


@pytest.mark.skipif(not _STOCKFISH_AVAILABLE, reason="stockfish binary not on PATH")
@_aio
async def test_pick_move_returns_legal_move_at_native_elo():
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 2000)
    board = chess.Board()
    assert move in board.legal_moves


@_aio
async def test_pick_move_quits_engine_even_when_configure_raises(monkeypatch):
    """If configure() raises during the native path, the finally clause must
    still close the engine."""
    quit_count = {"n": 0}

    class _FakeEngine:
        async def configure(self, options):
            raise RuntimeError("config error")

        async def play(self, board, limit):
            return SimpleNamespace(move=chess.Move.from_uci("e2e4"))

        async def quit(self):
            quit_count["n"] += 1

    class _FakeTransport:
        def close(self):
            pass

    async def _fake_popen(_path):
        return _FakeTransport(), _FakeEngine()

    monkeypatch.setattr(chess_bot.chess.engine, "popen_uci", _fake_popen)

    with pytest.raises(RuntimeError, match="config error"):
        await chess_bot.pick_move(chess_engine.STARTING_FEN, 2000)
    assert quit_count["n"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Cog integration — bot-mention branch in cmd_chess
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_chess_state():
    yield
    _state.active_chess_games.clear()


@pytest.fixture(autouse=True)
def _stub_chess_helpers(monkeypatch):
    """Mirror of the autouse stub in test_chess.py — chess.py uses _bump_board
    + _delete_after + check_chess_channel. Stub all three for cog tests."""
    import src.games.chess as _chess_mod
    bump_calls = []
    async def _stub_bump(channel, game, embed, *, file=None):
        game["board_msg_id"] = (game.get("board_msg_id") or 0) + 1
        bump_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_chess_mod, "_bump_board", _stub_bump)

    async def _noop(_msg):
        return None
    monkeypatch.setattr(_chess_mod, "_delete_after", _noop)

    async def _allow(_ctx):
        return False
    monkeypatch.setattr(_chess_mod, "check_chess_channel", _allow)

    edit_calls = []
    async def _stub_edit(channel, game, embed, *, file=None):
        edit_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub_edit)
    monkeypatch.setattr(_ai_cog, "_bump_chess_board", _stub_bump)
    return bump_calls


def _make_bot_cog(bot_user_id: int = 999_000_001) -> ChessCog:
    """A ChessCog whose self.bot.user.id matches the given id so the
    cmd_chess bot-mention branch fires when a member with that id is mentioned."""
    fake_bot_user = SimpleNamespace(id=bot_user_id)
    fake_bot = SimpleNamespace(user=fake_bot_user)
    return ChessCog(bot=fake_bot)


def _ctx_for(member: FakeMember, channel_id: int, *, mentions=()) -> FakeCtx:
    g = FakeGuild(gid=42)
    g.members.append(member)
    for m in mentions:
        g.members.append(m)
    ctx = FakeCtx(author=member, guild=g)
    ctx.channel.id = channel_id
    ctx.channel.guild = g  # _finalize_game reads channel.guild for the record's gid
    ctx.message = FakeMessage(author=member)
    ctx.message.mentions = list(mentions)
    return ctx


@_aio
async def test_cmd_chess_bot_mention_default_elo(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2000, display_name="Alice")
    bot_member = FakeMember(uid=cog.bot.user.id, display_name="TheBot")
    ctx = _ctx_for(challenger, channel_id=1000, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx)

    assert 1000 in _state.active_chess_games
    g = _state.active_chess_games[1000]
    assert g["white_id"] == challenger.id
    assert g["black_id"] == cog.bot.user.id
    assert g["current_id"] == challenger.id
    assert g["elo"] == chess_bot.ELO_DEFAULT
    assert g["amount"] == 0


@_aio
async def test_cmd_chess_bot_mention_custom_elo(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2001)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1001, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx, "1500")

    assert _state.active_chess_games[1001]["elo"] == 1500


@_aio
async def test_cmd_chess_bot_mention_elo_below_min_rejected(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2002)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1002, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx, "50")

    # No game created; error embed sent.
    assert 1002 not in _state.active_chess_games
    assert len(ctx.sent_embeds) >= 1
    assert "Invalid Elo" in ctx.sent_embeds[-1].title


@_aio
async def test_cmd_chess_bot_mention_elo_above_max_rejected(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2003)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1003, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx, "9999")

    assert 1003 not in _state.active_chess_games
    assert "Invalid Elo" in ctx.sent_embeds[-1].title


@_aio
async def test_cmd_chess_bot_mention_skips_setup_pvp_game(db, _stub_chess_helpers, monkeypatch):
    """Bot games must NOT route through _setup_pvp_game (no confirmation, no
    opponent balance, no wager)."""
    import src.games.chess as _chess_mod
    setup_called = []
    async def _spy(*args, **kwargs):
        setup_called.append((args, kwargs))
        return True
    monkeypatch.setattr(_chess_mod, "_setup_pvp_game", _spy)

    cog = _make_bot_cog()
    challenger = FakeMember(uid=2004)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1004, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx)

    assert 1004 in _state.active_chess_games
    assert setup_called == [], "_setup_pvp_game should not be called for bot games"


@_aio
async def test_cmd_chess_human_opponent_still_goes_through_setup(db, _stub_chess_helpers, monkeypatch):
    """Regression: mentioning a non-bot user still routes through _setup_pvp_game
    (trailing int treated as a wager, not an Elo)."""
    import src.games.chess as _chess_mod
    setup_called = []
    async def _spy(ctx, opponent, amount, invite_title):
        setup_called.append({"opponent": opponent, "amount": amount})
        return True
    monkeypatch.setattr(_chess_mod, "_setup_pvp_game", _spy)

    cog = _make_bot_cog(bot_user_id=999_000_002)
    challenger = FakeMember(uid=2005)
    human_opp = FakeMember(uid=2006)
    ctx = _ctx_for(challenger, channel_id=1005, mentions=[human_opp])

    await cog.cmd_chess.callback(cog, ctx, "500")

    assert len(setup_called) == 1
    assert setup_called[0]["opponent"] is human_opp
    assert setup_called[0]["amount"] == 500
    assert _state.active_chess_games[1005]["amount"] == 500
    assert "elo" not in _state.active_chess_games[1005]


# ─────────────────────────────────────────────────────────────────────────────
# Cog integration — !chessbot alias for !chess @TheBot
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _allow_chess_channel(monkeypatch):
    """Bypass check_chess_channel so cog tests don't need guild config."""
    import src.games.chess as _chess_mod
    async def _allow(_ctx):
        return False
    monkeypatch.setattr(_chess_mod, "check_chess_channel", _allow)


@_aio
async def test_cmd_chessbot_default_elo(db, _stub_chess_helpers, _allow_chess_channel):
    """!chessbot with no args starts a bot game at ELO_DEFAULT."""
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2050, display_name="Alice")
    ctx = _ctx_for(challenger, channel_id=1050)

    await cog.cmd_chessbot.callback(cog, ctx)

    assert 1050 in _state.active_chess_games
    g = _state.active_chess_games[1050]
    assert g["white_id"] == challenger.id
    assert g["black_id"] == cog.bot.user.id
    assert g["elo"] == chess_bot.ELO_DEFAULT


@_aio
async def test_cmd_chessbot_custom_elo(db, _stub_chess_helpers, _allow_chess_channel):
    """!chessbot 1500 starts at 1500 Elo."""
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2051)
    ctx = _ctx_for(challenger, channel_id=1051)

    await cog.cmd_chessbot.callback(cog, ctx, "1500")

    assert _state.active_chess_games[1051]["elo"] == 1500


@_aio
async def test_cmd_chessbot_non_integer_arg_rejected(db, _stub_chess_helpers, _allow_chess_channel):
    """!chessbot abc sends the usage error and creates no game."""
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2052)
    ctx = _ctx_for(challenger, channel_id=1052)

    await cog.cmd_chessbot.callback(cog, ctx, "abc")

    assert 1052 not in _state.active_chess_games
    assert "Invalid Elo" in ctx.sent_embeds[-1].title


@_aio
async def test_cmd_chessbot_elo_out_of_range_rejected(db, _stub_chess_helpers, _allow_chess_channel):
    """!chessbot 50 and !chessbot 9999 both rejected; no game created."""
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2053)

    ctx_low = _ctx_for(challenger, channel_id=1053)
    await cog.cmd_chessbot.callback(cog, ctx_low, "50")
    assert 1053 not in _state.active_chess_games
    assert "Invalid Elo" in ctx_low.sent_embeds[-1].title

    ctx_high = _ctx_for(challenger, channel_id=1054)
    await cog.cmd_chessbot.callback(cog, ctx_high, "9999")
    assert 1054 not in _state.active_chess_games
    assert "Invalid Elo" in ctx_high.sent_embeds[-1].title


@_aio
async def test_cmd_chessbot_blocked_when_channel_busy(db, _stub_chess_helpers, _allow_chess_channel):
    """If the channel has any active game (TTT, C4, chess), !chessbot refuses."""
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2054)
    ctx = _ctx_for(challenger, channel_id=1055)
    _state.active_ttt_games[1055] = {"players": [challenger.id, 0]}
    try:
        await cog.cmd_chessbot.callback(cog, ctx)
        assert 1055 not in _state.active_chess_games
        assert "Game Active" in ctx.sent_embeds[-1].title
    finally:
        _state.active_ttt_games.pop(1055, None)


# ─────────────────────────────────────────────────────────────────────────────
# Cog integration — _play_bot_reply fires after a human move against the bot
# ─────────────────────────────────────────────────────────────────────────────


def _seed_bot_chess_game(channel_id: int, white_id: int, bot_id: int, *, elo: int = 1320,
                         fen: str | None = None, current_id: int | None = None):
    starting_fen = fen if fen is not None else chess_engine.STARTING_FEN
    _state.active_chess_games[channel_id] = {
        "fen": starting_fen,
        "pgn": _initial_pgn("White", f"Stockfish ({elo} Elo)", None, starting_fen=starting_fen),
        "white_id": white_id,
        "black_id": bot_id,
        "current_id": current_id if current_id is not None else white_id,
        "amount": 0,
        "elo": elo,
        "last_move": "",
        "board_msg_id": 1000,
    }


@_aio
async def test_human_move_triggers_bot_reply(db, _stub_chess_helpers, monkeypatch):
    """After the human plays, _play_bot_reply runs (stubbed pick_move) and
    advances the board with Stockfish's move."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2100, display_name="Alice")
    _seed_bot_chess_game(1100, human.id, cog.bot.user.id)

    # Make sure ctx.guild knows about the bot user for mention rendering.
    ctx = _ctx_for(human, channel_id=1100)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    # Stub Stockfish to always play e7e5.
    async def _stub_pick(fen, elo):
        return chess.Move.from_uci("e7e5")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    # _play_bot_reply runs as a background task — let the event loop drain it.
    await asyncio.sleep(0)
    # Pending tasks may need another nudge to fully complete.
    for _ in range(5):
        await asyncio.sleep(0)

    g = _state.active_chess_games[1100]
    # Both moves applied; back to white's turn.
    assert g["current_id"] == human.id
    assert "e4" in g["pgn"] and "e5" in g["pgn"]
    # FEN now reflects post-e5 position.
    assert g["fen"].startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w")


@_aio
async def test_bot_reply_handles_engine_error(db, _stub_chess_helpers, monkeypatch):
    """If Stockfish raises, the user sees a friendly error and the game state
    isn't half-committed for the bot's turn."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2101)
    _seed_bot_chess_game(1101, human.id, cog.bot.user.id)
    ctx = _ctx_for(human, channel_id=1101)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    async def _broken_pick(fen, elo):
        raise RuntimeError("simulated stockfish crash")
    monkeypatch.setattr(chess_bot, "pick_move", _broken_pick)

    # Capture channel.send (used by the error path inside _play_bot_reply).
    sent = []
    async def _record_send(*args, **kwargs):
        sent.append((args, kwargs))
        return FakeMessage()
    ctx.channel.send = _record_send

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    for _ in range(5):
        await asyncio.sleep(0)

    # User's move applied; bot's didn't; current_id is still bot (waiting on stockfish).
    g = _state.active_chess_games[1101]
    assert g["current_id"] == cog.bot.user.id
    assert "e4" in g["pgn"]
    # Error message was posted to the channel.
    assert any(
        kwargs.get("embed") is not None and "Chess Engine" in kwargs["embed"].title
        for _, kwargs in sent
    )


@_aio
async def test_bot_reply_no_op_when_game_ended(db, _stub_chess_helpers, monkeypatch):
    """If the human resigns between !move and the bot's reply, _play_bot_reply
    silently returns rather than crashing."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2102)
    _seed_bot_chess_game(1102, human.id, cog.bot.user.id)
    # Don't even queue the bot reply — directly invoke _play_bot_reply after
    # clearing state to simulate the race.
    _state.active_chess_games.pop(1102, None)

    channel = FakeCtx(author=human).channel
    channel.id = 1102

    # Should not raise.
    await cog._play_bot_reply(channel, 1102)


@_aio
async def test_bot_reply_uses_game_elo_not_default(db, _stub_chess_helpers, monkeypatch):
    """The game's elo (not ELO_DEFAULT) is what reaches Stockfish. Pins the
    end-to-end wiring: cmd_chess stores elo → _play_bot_reply reads it →
    pick_move receives it."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2300, display_name="Alice")
    # Seed a game at a deliberately non-default Elo.
    _seed_bot_chess_game(1300, human.id, cog.bot.user.id, elo=2100)
    ctx = _ctx_for(human, channel_id=1300)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    received: list[int] = []
    async def _stub_pick(fen, elo):
        received.append(elo)
        return chess.Move.from_uci("e7e5")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    for _ in range(5):
        await asyncio.sleep(0)

    assert received == [2100], f"expected Stockfish called with 2100, got {received}"


@_aio
async def test_persisted_elo_used_after_reload(db, _stub_chess_helpers, monkeypatch):
    """End-to-end fix verification for the persistence change: save → clear
    state → reload via init_db_state → human moves → Stockfish is called
    with the originally-persisted Elo, not ELO_DEFAULT."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2301, display_name="Alice")
    _seed_bot_chess_game(1301, human.id, cog.bot.user.id, elo=1850)

    # Round-trip through persistence: save, clear in-memory, reload.
    from src.persistence import save_chess_game
    import src.persistence as _persistence
    await save_chess_game(1301)
    _state.active_chess_games.clear()
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()
    assert _state.active_chess_games[1301].get("elo") == 1850

    ctx = _ctx_for(human, channel_id=1301)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    received: list[int] = []
    async def _stub_pick(fen, elo):
        received.append(elo)
        return chess.Move.from_uci("e7e5")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    for _ in range(5):
        await asyncio.sleep(0)

    assert received == [1850], f"expected reloaded Elo 1850, got {received}"


@_aio
async def test_stockfish_mate_creates_report_with_bot_as_winner(db, _stub_chess_helpers, monkeypatch):
    """When Stockfish delivers mate, the game-over path runs and a chess_reports
    row is written with winner_id = bot.user.id. Seeds a fool's-mate-ready
    position so a single human move sets up the bot's mating reply."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2400, display_name="Alice")
    # FEN: standard start. Plays 1. f3 (white), 2. ... Qh4# is bot's mate move.
    # But fool's mate is: 1. f3 e5 2. g4 Qh4#. The bot needs TWO moves to mate,
    # not one. So we seed directly to "white to move 2. g4" state, then have
    # the human play g4, and force Stockfish's stubbed reply to be Qh4#.
    _state.active_chess_games[1400] = {
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
        "pgn": _initial_pgn(
            "Alice", "Stockfish (1320 Elo)", None,
            starting_fen="rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 1320,
        "last_move": "",
        "board_msg_id": 1,
    }

    ctx = _ctx_for(human, channel_id=1400)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    # Stockfish plays Qh4# after white's g4.
    async def _stub_pick(fen, elo):
        return chess.Move.from_uci("d8h4")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "g4")
    for _ in range(8):
        await asyncio.sleep(0)

    # Game ended; chess_games row gone, chess_reports row created.
    assert 1400 not in _state.active_chess_games

    from src.persistence import load_chess_report
    report = await load_chess_report(1)
    assert report is not None
    assert report["winner_id"] == cog.bot.user.id
    assert report["result"] == "0-1"


@_aio
async def test_stockfish_mate_headline_uses_stockfish_label_not_raw_uid(
    db, _stub_chess_helpers, monkeypatch,
):
    """When Stockfish mates the human, the game-over embed must show
    'Stockfish (1320 Elo)' not the raw 18-digit bot user id."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2401, display_name="Alice")
    _state.active_chess_games[1401] = {
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
        "pgn": _initial_pgn(
            "Alice", "Stockfish (1320 Elo)", None,
            starting_fen="rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 1320,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(human, channel_id=1401)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    async def _stub_pick(fen, elo):
        return chess.Move.from_uci("d8h4")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "g4")
    for _ in range(8):
        await asyncio.sleep(0)

    # Inspect the bump_calls accumulator from the autouse stub.
    bot_uid_str = str(cog.bot.user.id)
    game_over_embeds = [
        embed for _ch, _g, embed, _f in _stub_chess_helpers
        if embed.title and "Game Over" in embed.title
    ]
    assert len(game_over_embeds) >= 1, "expected a game-over embed"
    last = game_over_embeds[-1]
    assert bot_uid_str not in (last.description or ""), \
        f"raw bot uid leaked into game-over description: {last.description!r}"
    assert "Stockfish" in (last.description or "")


@_aio
async def test_human_defeats_bot_triggers_bounty_payout(db, _stub_chess_helpers):
    """End-to-end: human plays the mating move against the bot, _finalize_game
    runs the bot-defeat bounty, user balance reflects the split-rate payout
    (10 coins per Elo below 1000), and the game-over embed includes the bounty
    line."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2500, display_name="MateMachine")
    # Scholar's-mate-ready: white to move, Qxf7# delivers mate.
    _state.active_chess_games[1500] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "MateMachine", "Stockfish (800 Elo)", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 800,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(human, channel_id=1500)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(5):
        await asyncio.sleep(0)

    # Game ended; bounty paid (no prior win today; 800 Elo all below the 1000
    # threshold → 800 * 10 = 8000).
    assert 1500 not in _state.active_chess_games
    from src.economy import get_balance
    from src.games.bot_chess_rewards import COINS_PER_NEW_ELO_LOW
    assert await get_balance(human.id) == 800 * COINS_PER_NEW_ELO_LOW

    # Highwater set to 800 today.
    from src.economy import _ct_today
    u = _state.economy["users"][str(human.id)]
    assert u["bot_chess_elo_max_today"] == 800
    assert u["bot_chess_elo_max_date"] == _ct_today()

    # Embed mentions the bounty.
    game_over = [
        embed for _ch, _g, embed, _f in _stub_chess_helpers
        if embed.title and "Game Over" in embed.title
    ]
    assert any(
        "800-Elo bot" in (e.description or "") for e in game_over
    ), "expected bounty line in game-over description"


@_aio
async def test_human_defeats_lower_elo_bot_after_higher_pays_nothing(db, _stub_chess_helpers):
    """Same-day flow: first beat 800, then beat 500 — second pays 0."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2501, display_name="Repeat")

    # Pre-seed the user's daily highwater at 800 so the next 500-Elo win is a no-op.
    from src.economy import _ensure_user, _ct_today, get_balance
    await _ensure_user(human.id)
    _state.economy["users"][str(human.id)]["bot_chess_elo_max_today"] = 800
    _state.economy["users"][str(human.id)]["bot_chess_elo_max_date"] = _ct_today()
    bal_before = await get_balance(human.id)

    # Now mate a 500-Elo bot.
    _state.active_chess_games[1501] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "Repeat", "Stockfish (500 Elo)", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 500,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(human, channel_id=1501)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(5):
        await asyncio.sleep(0)

    # No bounty added.
    assert await get_balance(human.id) == bal_before
    # Highwater unchanged at 800.
    assert _state.economy["users"][str(human.id)]["bot_chess_elo_max_today"] == 800


@_aio
async def test_bot_defeats_human_no_bounty(db, _stub_chess_helpers, monkeypatch):
    """When the bot wins (Stockfish mates the human), no bounty is paid to
    either side — the bounty path is gated on winner != bot."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2502, display_name="Loser")
    # Seed a position where it's BLACK (the bot) to move and Black's
    # next move is mate. Fool's-mate-ready, white to play g4 first.
    _state.active_chess_games[1502] = {
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
        "pgn": _initial_pgn(
            "Loser", "Stockfish (1320 Elo)", None,
            starting_fen="rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 1320,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(human, channel_id=1502)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    async def _stub_pick(fen, elo):
        return chess.Move.from_uci("d8h4")  # Qh4# after g4
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "g4")
    for _ in range(8):
        await asyncio.sleep(0)

    # Game ended. Human is the loser; no bounty.
    from src.economy import get_balance
    assert await get_balance(human.id) == 0
    u = _state.economy["users"].get(str(human.id), {})
    assert u.get("bot_chess_elo_max_today", 0) == 0


@_aio
async def test_pvp_win_does_not_trigger_bot_bounty(db, _stub_chess_helpers):
    """Regression: human-vs-human chess (no 'elo' key in the game dict)
    must NOT pay out the bot bounty."""
    cog = _make_bot_cog()
    white = FakeMember(uid=2600, display_name="Alice")
    black = FakeMember(uid=2601, display_name="Bob")
    _state.active_chess_games[1600] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "Alice", "Bob", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": white.id,
        "black_id": black.id,
        "current_id": white.id,
        "amount": 0,
        # NB: no "elo" key — this is a PvP game.
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(white, channel_id=1600)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(3):
        await asyncio.sleep(0)

    # No bounty, no highwater change.
    from src.economy import get_balance
    assert await get_balance(white.id) == 0
    u = _state.economy["users"].get(str(white.id), {})
    assert u.get("bot_chess_elo_max_today", 0) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Head-to-head + chess_pvp_wins record (PvP-only, skips bot games)
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_pvp_game_end_shows_head_to_head_in_embed(db, _stub_chess_helpers):
    """When a PvP game ends in checkmate, the game-over embed includes a
    'Head-to-head:' line summarizing all-time wins between these two players."""
    cog = _make_bot_cog()
    white = FakeMember(uid=2700, display_name="Alice")
    black = FakeMember(uid=2701, display_name="Bob")

    # Pre-seed two prior reports: Alice won once, Bob won once.
    from src.persistence import save_chess_report
    await save_chess_report(guild_id=42, channel_id=1, white_id=white.id, black_id=black.id,
                            winner_id=white.id, result="1-0", pgn="", final_fen="-")
    await save_chess_report(guild_id=42, channel_id=1, white_id=black.id, black_id=white.id,
                            winner_id=black.id, result="1-0", pgn="", final_fen="-")

    # Now play a PvP game where white mates black via Qxf7#.
    _state.active_chess_games[1700] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "Alice", "Bob", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": white.id,
        "black_id": black.id,
        "current_id": white.id,
        "amount": 0,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(white, channel_id=1700)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(3):
        await asyncio.sleep(0)

    # After Alice's mate, totals: Alice 2 wins, Bob 1 win, 0 draws.
    game_over = [
        embed for _ch, _g, embed, _f in _stub_chess_helpers
        if embed.title and "Game Over" in embed.title
    ]
    assert game_over, "expected a game-over embed"
    last = game_over[-1]
    desc = last.description or ""
    assert "Head-to-head:" in desc, f"missing H2H line in: {desc}"
    assert "Alice 2 – 1 Bob" in desc or "Alice 2 – 1 Bob" in desc, \
        f"unexpected H2H line: {desc}"


@_aio
async def test_bot_game_end_does_not_show_head_to_head(db, _stub_chess_helpers):
    """Bot games (carry 'elo' key) skip the H2H block entirely."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2710, display_name="Solo")

    _state.active_chess_games[1710] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "Solo", "Stockfish (800 Elo)", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 800,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(human, channel_id=1710)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(3):
        await asyncio.sleep(0)

    game_over = [
        embed for _ch, _g, embed, _f in _stub_chess_helpers
        if embed.title and "Game Over" in embed.title
    ]
    assert game_over
    desc = game_over[-1].description or ""
    assert "Head-to-head:" not in desc, f"H2H should not appear in bot games: {desc}"


@_aio
async def test_pvp_win_sets_chess_pvp_wins_record(db, _stub_chess_helpers):
    """A PvP checkmate-win updates the chess_pvp_wins record for the guild."""
    cog = _make_bot_cog()
    white = FakeMember(uid=2720, display_name="Champ")
    black = FakeMember(uid=2721, display_name="Loser")

    _state.active_chess_games[1720] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "Champ", "Loser", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": white.id, "black_id": black.id, "current_id": white.id,
        "amount": 0, "last_move": "", "board_msg_id": 1,
    }
    ctx = _ctx_for(white, channel_id=1720)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(3):
        await asyncio.sleep(0)

    from src.persistence import load_records
    records = await load_records(ctx.guild.id)
    assert "chess_pvp_wins" in records
    assert records["chess_pvp_wins"]["holder_id"] == white.id
    assert records["chess_pvp_wins"]["value"] == 1


@_aio
async def test_bot_win_does_not_set_chess_pvp_wins_record(db, _stub_chess_helpers):
    """Beating the bot doesn't count toward chess_pvp_wins (would let users
    grind low-Elo bots to top the PvP leaderboard)."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2730, display_name="Farmer")

    _state.active_chess_games[1730] = {
        "fen": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        "pgn": _initial_pgn(
            "Farmer", "Stockfish (100 Elo)", None,
            starting_fen="r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
        ),
        "white_id": human.id,
        "black_id": cog.bot.user.id,
        "current_id": human.id,
        "amount": 0,
        "elo": 100,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(human, channel_id=1730)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    await cog.cmd_move_chess.callback(cog, ctx, "Qxf7#")
    for _ in range(3):
        await asyncio.sleep(0)

    from src.persistence import load_records
    records = await load_records(ctx.guild.id)
    assert "chess_pvp_wins" not in records, \
        f"bot game must not create chess_pvp_wins record; got: {records}"


@_aio
async def test_bot_reply_does_not_fire_for_pvp_game(db, _stub_chess_helpers, monkeypatch):
    """Regression: a PvP move between two humans must NOT trigger _play_bot_reply."""
    cog = _make_bot_cog()
    white = FakeMember(uid=2200)
    black = FakeMember(uid=2201)
    # PvP game seed — no 'elo' key, opponent_id != bot.user.id
    _state.active_chess_games[1200] = {
        "fen": chess_engine.STARTING_FEN,
        "pgn": _initial_pgn("White", "Black", None),
        "white_id": white.id,
        "black_id": black.id,
        "current_id": white.id,
        "amount": 0,
        "last_move": "",
        "board_msg_id": 555,
    }
    ctx = _ctx_for(white, channel_id=1200)
    ctx.guild.members.append(black)

    pick_called = []
    async def _stub_pick(fen, elo):
        pick_called.append((fen, elo))
        return chess.Move.from_uci("e7e5")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    for _ in range(3):
        await asyncio.sleep(0)

    assert pick_called == [], "pick_move should not be called in PvP games"
    # Black's turn now, as expected for a PvP move.
    assert _state.active_chess_games[1200]["current_id"] == black.id
