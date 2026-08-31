"""Post-game chess analysis: stat math, cheat flagging, and the
analyze_and_post pipeline (with the engine seam stubbed — no Stockfish
binary is required by any test here)."""
from unittest.mock import AsyncMock

import chess
import chess.pgn
import pytest

import src.state as _state
from src.games import chess_analysis as ca
from src.persistence.chess import save_chess_report, load_chess_report, count_flagged_reports

from tests.fakes.discord import FakeChannel, FakeGuild

pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────────────
# Pure math: win% conversion and avg win%-loss → est. Elo
# ─────────────────────────────────────────────────────────────────────────────


async def test_estimate_elo_anchors_and_clamps():
    assert ca.estimate_elo_from_awpl(0) == 2900      # clamped at the top
    assert ca.estimate_elo_from_awpl(0.5) == 2900
    assert ca.estimate_elo_from_awpl(5.0) == 1300    # exact anchor (SF@1320 point)
    assert ca.estimate_elo_from_awpl(9.5) == 600     # degraded-Maia@600 point
    assert ca.estimate_elo_from_awpl(30) == 100      # clamped at the bottom


async def test_estimate_elo_is_monotonic_and_rounded():
    prev = None
    for tenths in range(0, 300, 5):
        est = ca.estimate_elo_from_awpl(tenths / 10.0)
        assert est % 50 == 0
        if prev is not None:
            assert est <= prev
        prev = est


async def test_win_pct_grades_by_context_not_raw_centipawns():
    """The v1→v2 motivation: the same cp swing costs a lot in an equal
    position and almost nothing in a decided one — clamped-cp ACPL graded
    the decided-position blunder as zero and inflated weak games to ~1500+."""
    assert ca.win_pct(0) == pytest.approx(50.0)
    assert ca.win_pct(0) - ca.win_pct(-300) > 20      # blunder while equal
    assert ca.win_pct(900) - ca.win_pct(600) < 8      # same swing, decided game
    # Perfect move → ~100% accuracy; torching 30 win% → very low.
    assert ca.move_accuracy_pct(0) == pytest.approx(100.0, abs=0.1)
    assert ca.move_accuracy_pct(30) < 25.0


# ─────────────────────────────────────────────────────────────────────────────
# summarize_evals
# ─────────────────────────────────────────────────────────────────────────────


def _row(color, matched, loss, trivial=False, wp_loss=0.0, weight=1.0):
    return {"color": color, "matched": matched, "loss": loss,
            "wp_loss": wp_loss, "weight": weight, "trivial": trivial}


async def test_summarize_computes_acpl_and_match_over_nontrivial_only():
    evals = [
        _row("white", True, 0, wp_loss=0.0),
        _row("white", True, 20, wp_loss=2.0),
        _row("white", False, 40, trivial=True, wp_loss=4.0),  # excluded from match rate
        _row("black", False, 100, wp_loss=10.0),
        _row("black", False, 200, wp_loss=20.0),
    ]
    a = ca.summarize_evals(
        evals, white_seconds=30, black_seconds=0,
        white_move_total=10, black_move_total=10,
    )
    w, b = a["white"], a["black"]
    assert w["moves"] == 3 and w["nontrivial"] == 2
    assert w["acpl"] == pytest.approx(20.0)          # (0+20+40)/3
    assert w["awpl"] == pytest.approx(2.0)           # (0+2+4)/3, weight 1 each
    assert w["est_elo"] is None                      # 3 eff. moves < the floor
    assert 0 < w["accuracy"] <= 100
    assert w["match_pct"] == pytest.approx(100.0)    # 2/2 non-trivial matched
    assert w["avg_seconds"] == pytest.approx(3.0)
    assert b["acpl"] == pytest.approx(150.0)
    assert b["awpl"] == pytest.approx(15.0)
    assert b["accuracy"] < w["accuracy"]
    assert b["match_pct"] == pytest.approx(0.0)
    assert b["avg_seconds"] is None                  # no clock data


async def test_weak_game_with_clamped_blunders_estimates_weak():
    """Regression for 600-level games estimating ~1500-1800: long decided
    stretches where the cp clamp hid blunders as 0 loss diluted ACPL, and
    est_elo came out strong. Graded by win% lost, the equal-position queen
    hangs dominate and the estimate lands where it belongs."""
    rows = []
    for i in range(20):
        if i % 4 == 0:    # queen hang while roughly equal: ~45 win% torched
            rows.append(_row("white", False, 800,
                             wp_loss=ca.win_pct(0) - ca.win_pct(-800)))
        elif i % 2 == 0:  # flailing in decided positions: clamp made these 0 cp
            rows.append(_row("white", False, 0, wp_loss=0.5))
        else:
            rows.append(_row("white", False, 60, wp_loss=5.0))
    a = ca.summarize_evals(rows)
    assert a["white"]["est_elo"] <= 1000


async def test_est_elo_stake_weighting_and_floor():
    """A game decided early must not read as flawless: decided-phase plies
    carry ~zero stake weight, so they no longer dilute the estimate (the
    unweighted mean here would be ~2.3 awpl → ~2200 Elo)."""
    live = [_row("white", False, 300, wp_loss=25.0, weight=1.0) for _ in range(3)]
    live += [_row("white", False, 100, wp_loss=8.0, weight=0.8) for _ in range(2)]
    mopup = [_row("white", False, 0, wp_loss=0.2, weight=0.0) for _ in range(30)]
    a = ca.summarize_evals(live + mopup)
    w = a["white"]
    expected = (25.0 * 3 + 8.0 * 0.8 * 2) / (3 + 1.6)
    assert w["awpl"] == pytest.approx(expected, abs=0.01)
    assert w["est_elo"] == ca.estimate_elo_from_awpl(expected)
    assert w["est_elo"] <= 500

    # Nothing contested at all → below the effective-sample floor: accuracy
    # still reported, but no Elo estimate.
    a2 = ca.summarize_evals(mopup)
    assert a2["white"]["est_elo"] is None
    assert a2["white"]["accuracy"] is not None


async def test_stake_weight_shape():
    assert ca.stake_weight(50.0) == pytest.approx(1.0)
    assert ca.stake_weight(2.5) == 0.0     # decided either way → zero weight
    assert ca.stake_weight(97.5) == 0.0
    assert 0.0 < ca.stake_weight(80.0) < ca.stake_weight(60.0) < 1.0


async def test_summarize_handles_side_with_no_rows():
    a = ca.summarize_evals([_row("white", True, 10)])
    assert a["black"] == {"moves": 0}
    assert a["white"]["moves"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# flag_suspect
# ─────────────────────────────────────────────────────────────────────────────

_BOT_ID = 999
_HUMAN = 10


def _suspicious_analysis(acpl=10.0, match=85.0, nontrivial=20):
    return {
        "version": 2, "depth": 12,
        "white": {"moves": 25, "nontrivial": nontrivial, "acpl": acpl,
                  "awpl": 0.8, "eff_moves": 12.0, "accuracy": 97.4,
                  "match_pct": match,
                  "est_elo": ca.estimate_elo_from_awpl(0.8),  # 2800
                  "avg_seconds": 5.0},
        "black": {"moves": 25, "nontrivial": 20, "acpl": 60.0,
                  "awpl": 5.5, "eff_moves": 12.0, "accuracy": 79.0,
                  "match_pct": 30.0, "est_elo": 1600, "avg_seconds": None},
    }


def _flag(analysis, *, winner=_HUMAN, bot_elo=1500):
    return ca.flag_suspect(
        analysis, white_id=_HUMAN, black_id=_BOT_ID,
        winner_id=winner, bot_user_id=_BOT_ID, bot_elo=bot_elo,
    )


async def test_flags_engine_perfect_win_vs_strong_bot():
    assert _flag(_suspicious_analysis()) == _HUMAN


async def test_flags_draw_but_not_loss():
    assert _flag(_suspicious_analysis(), winner=None) == _HUMAN
    assert _flag(_suspicious_analysis(), winner=_BOT_ID) is None


async def test_no_flag_below_bot_elo_threshold_or_pvp():
    assert _flag(_suspicious_analysis(), bot_elo=1000) is None
    assert ca.flag_suspect(
        _suspicious_analysis(), white_id=_HUMAN, black_id=11,
        winner_id=_HUMAN, bot_user_id=_BOT_ID, bot_elo=None,  # PvP: no elo
    ) is None


async def test_no_flag_when_stats_are_human():
    assert _flag(_suspicious_analysis(acpl=40.0)) is None          # sloppy play
    assert _flag(_suspicious_analysis(match=50.0)) is None         # low match
    assert _flag(_suspicious_analysis(nontrivial=10)) is None      # too few moves


async def test_flag_thresholds_are_inclusive():
    a = _suspicious_analysis(
        acpl=ca.FLAG_MAX_ACPL, match=ca.FLAG_MIN_MATCH_PCT,
        nontrivial=ca.FLAG_MIN_NONTRIVIAL,
    )
    assert _flag(a) == _HUMAN


# ─────────────────────────────────────────────────────────────────────────────
# _collect_move_evals (fake evaluator — no engine)
# ─────────────────────────────────────────────────────────────────────────────


# 24 legal plies of Ruy Lopez (Breyer) — a real line, so no automatic
# repetition/fivefold draw can terminate the game mid-analysis the way a
# synthetic piece-shuffle would.
_LINE = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6",
    "e1g1", "f8e7", "f1e1", "b7b5", "a4b3", "d7d6", "c2c3", "e8g8",
    "h2h3", "c6b8", "d2d4", "b8d7", "b1d2", "c8b7", "b3c2", "f8e8",
]


def _line_game(plies: int = len(_LINE)) -> tuple[str, list[chess.Move]]:
    """(pgn, moves) for the first `plies` half-moves of the fixture line."""
    board = chess.Board()
    game = chess.pgn.Game()
    node = game
    moves = []
    for uci in _LINE[:plies]:
        move = chess.Move.from_uci(uci)
        board.push(move)
        node = node.add_variation(move)
        moves.append(move)
    return str(game), moves


def _pgn_fixture(plies: int = len(_LINE)) -> str:
    return _line_game(plies)[0]


async def test_collect_grades_matches_and_losses():
    pgn, moves = _line_game()

    def _played_or_any(board):
        ply = board.ply()
        return moves[ply] if ply < len(moves) else next(iter(board.legal_moves))

    # Evaluator A: best move IS the played move, flat 0 evals → every ply
    # matched, zero loss, non-trivial.
    async def eval_match(board):
        return _played_or_any(board), 0, -50

    evals = await ca._collect_move_evals(pgn, eval_match)
    assert evals is not None
    assert len(evals) == len(moves) - ca.BOOK_PLIES
    assert all(e["matched"] and e["loss"] == 0 and e["wp_loss"] == 0
               and not e["trivial"] for e in evals)
    # Colors alternate starting with whoever moves at ply BOOK_PLIES.
    first_color = "white" if ca.BOOK_PLIES % 2 == 0 else "black"
    assert evals[0]["color"] == first_color

    # Evaluator B: best move is something else, with a flat +50 eval for
    # every position. Nothing matches, and because the +50 is from the
    # side-to-move's POV each played move grades as a 100cp loss
    # (best 50 vs played −50 after the POV flip).
    async def eval_miss(board):
        played = _played_or_any(board)
        other = next(m for m in board.legal_moves if m != played)
        return other, 50, 0

    evals = await ca._collect_move_evals(pgn, eval_miss)
    expected_wp = ca.win_pct(50) - ca.win_pct(-50)
    assert all(
        (not e["matched"]) and e["loss"] == 100
        and e["wp_loss"] == pytest.approx(expected_wp)
        for e in evals
    )


async def test_collect_marks_trivial_on_wide_gap():
    pgn, moves = _line_game()

    async def eval_gapped(board):
        ply = board.ply()
        played = moves[ply] if ply < len(moves) else next(iter(board.legal_moves))
        # Runner-up trails by more than TRIVIAL_GAP_CP → trivial position.
        return played, 0, -(ca.TRIVIAL_GAP_CP + 1)

    evals = await ca._collect_move_evals(pgn, eval_gapped)
    assert all(e["trivial"] for e in evals)


async def test_collect_rejects_short_games():
    async def eval_never(board):  # pragma: no cover - must not be called
        raise AssertionError("evaluate called for a too-short game")

    assert await ca._collect_move_evals(_pgn_fixture(ca.MIN_PLIES_FOR_ANALYSIS - 2), eval_never) is None


async def test_collect_handles_checkmate_terminal(monkeypatch):
    """A game ending in mate must not call the evaluator on the terminal
    position (fool's mate, with the book/min gates lowered to reach it)."""
    monkeypatch.setattr(ca, "BOOK_PLIES", 0)
    monkeypatch.setattr(ca, "MIN_PLIES_FOR_ANALYSIS", 4)
    board = chess.Board()
    game = chess.pgn.Game()
    node = game
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
        move = chess.Move.from_uci(uci)
        board.push(move)
        node = node.add_variation(move)
    assert board.is_checkmate()

    async def eval_flat(b):
        assert not b.is_game_over()
        return next(iter(b.legal_moves)), 0, 0

    evals = await ca._collect_move_evals(str(game), eval_flat)
    assert evals is not None and len(evals) == 4
    # Black's mating move: played_cp = CP_CAP (opponent is mated) → 0 loss.
    assert evals[-1]["color"] == "black" and evals[-1]["loss"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# analyze_and_post pipeline (engine seam stubbed with canned analysis)
# ─────────────────────────────────────────────────────────────────────────────


class _StubBotUser:
    def __init__(self, uid=_BOT_ID):
        self.id = uid


class _StubBot:
    def __init__(self, log_channel=None):
        self.user = _StubBotUser()
        self._log_channel = log_channel

    def get_channel(self, cid):
        return self._log_channel


async def _seed_report(*, elo, winner_id=_HUMAN):
    return await save_chess_report(
        guild_id=42, channel_id=100, white_id=_HUMAN, black_id=_BOT_ID,
        winner_id=winner_id, result="0-1" if winner_id == _BOT_ID else "1-0",
        pgn=_pgn_fixture(), final_fen=chess.STARTING_FEN,
        elo=elo,
    )


async def _run_pipeline(monkeypatch, *, analysis, elo, report_id, log_channel):
    async def _canned(*args, **kwargs):
        return analysis

    monkeypatch.setattr(ca, "_run_engine_analysis", _canned)
    channel = FakeChannel(ch_id=100)
    channel.mention = "#chess"
    await ca.analyze_and_post(
        bot=_StubBot(log_channel=log_channel), channel=channel,
        guild=FakeGuild(gid=42), report_id=report_id,
        pgn=_pgn_fixture(),
        white_id=_HUMAN, black_id=_BOT_ID, winner_id=_HUMAN, elo=elo,
        white_name="Suspect", black_name="Maia (1500 Elo)",
        white_seconds=120, black_seconds=60, embed_msg_id=None,
    )
    return channel


async def test_pipeline_persists_flags_and_alerts(db, monkeypatch):
    monkeypatch.setitem(_state.bot_settings, "admin_log_channel", "555")
    log_channel = FakeChannel(ch_id=555)
    report_id = await _seed_report(elo=1500)

    channel = await _run_pipeline(
        monkeypatch, analysis=_suspicious_analysis(), elo=1500,
        report_id=report_id, log_channel=log_channel,
    )

    # Persisted: analysis + flag on the report row, visible to !chess view.
    report = await load_chess_report(report_id)
    assert report["analysis"]["white"]["acpl"] == 10.0
    assert report["flag_user_id"] == _HUMAN
    assert await count_flagged_reports(_HUMAN) == 1

    # Game channel got the analysis embed (no game-over embed to edit here).
    game_embed = channel.send.call_args_list[0].kwargs["embed"]
    assert "Engine Analysis" in game_embed.description
    assert "~2,800 Elo" in game_embed.description  # awpl 0.8 → 2800

    # Admin-log channel got the cheat alert with the repeat count.
    alert = log_channel.send.call_args.kwargs["embed"]
    assert "Possible Chess Cheating" in alert.title
    assert "Suspect" in alert.description
    assert "**Flagged games (all-time):** 1" in alert.description


async def test_pipeline_no_flag_for_clean_or_pvp_games(db, monkeypatch):
    monkeypatch.setitem(_state.bot_settings, "admin_log_channel", "555")
    log_channel = FakeChannel(ch_id=555)

    # Clean (human-looking) stats vs a strong bot: analysis saved, no flag.
    rid = await _seed_report(elo=2000)
    await _run_pipeline(
        monkeypatch, analysis=_suspicious_analysis(acpl=55.0, match=40.0),
        elo=2000, report_id=rid, log_channel=log_channel,
    )
    report = await load_chess_report(rid)
    assert report["analysis"] is not None
    assert report["flag_user_id"] is None

    # PvP (elo=None): suspicious numbers alone never flag.
    rid2 = await _seed_report(elo=None)
    await _run_pipeline(
        monkeypatch, analysis=_suspicious_analysis(), elo=None,
        report_id=rid2, log_channel=log_channel,
    )
    assert (await load_chess_report(rid2))["flag_user_id"] is None
    log_channel.send.assert_not_called()


async def test_pipeline_edits_game_over_embed_in_place(db, monkeypatch):
    """When the game-over embed still exists, analysis is appended to it
    instead of posting a separate message."""
    import discord as _discord

    report_id = await _seed_report(elo=None)

    async def _canned(*args, **kwargs):
        return _suspicious_analysis()

    monkeypatch.setattr(ca, "_run_engine_analysis", _canned)
    channel = FakeChannel(ch_id=100)
    msg = AsyncMock()
    msg.embeds = [_discord.Embed(description="Game over.")]
    channel.fetch_message = AsyncMock(return_value=msg)

    await ca.analyze_and_post(
        bot=_StubBot(), channel=channel, guild=FakeGuild(gid=42),
        report_id=report_id, pgn=_pgn_fixture(),
        white_id=_HUMAN, black_id=11, winner_id=_HUMAN, elo=None,
        white_name="A", black_name="B", embed_msg_id=777,
    )

    edited = msg.edit.call_args.kwargs["embed"]
    assert "Game over." in edited.description
    assert "Engine Analysis" in edited.description
    channel.send.assert_not_called()  # edit succeeded → no follow-up embed


async def test_chess_view_renders_stored_analysis(db, monkeypatch):
    from src.games.chess import ChessCog
    from src.persistence.chess import save_chess_analysis
    from tests.fakes.discord import FakeCtx, FakeMember

    report_id = await _seed_report(elo=1500)
    await save_chess_analysis(report_id, _suspicious_analysis(), None)

    cog = ChessCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=1), guild=FakeGuild(gid=42))
    await cog._cmd_view(ctx, (str(report_id),))

    desc = ctx.sent_embeds[-1].description
    assert "Engine Analysis" in desc
    assert "~2,800 Elo" in desc
    assert "ACPL" in desc
