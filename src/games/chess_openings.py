"""Opening book for the Stockfish bot.

The bot is always black (v1 — see chess.py _start_bot_chess). Each opening is
a sequence of UCI move pairs the bot would play if the human cooperates;
the first move in each pair is white's expected reply and the second is the
bot's scripted black move. The bot plays a scripted move only when the
preceding history matches the opening's white moves verbatim.

If the human plays an unexpected move, the bot's opening is abandoned and
the normal sampled-move logic takes over for the rest of the game. The cog
also validates each book move against Stockfish's top-10 before playing it,
so genuinely terrible book moves get filtered out too.

Each opening declares an Elo range where it's "in character":
  - Mainstream openings (Italian, Queen's Gambit Declined, etc.) are common
    at Elo 400+.
  - Aggressive/early-queen attacks (Scandinavian, Englund Gambit) are most
    common at Elo 300-500 where humans rely on tactics over development.
  - Traps (Fried Liver victim line, Légal Trap setups) sit in mid-range.
  - Joke/offbeat openings (Bongcloud, Hippopotamus) are rare overall but
    have wider Elo bands for variety.

Adding an opening: append to OPENINGS with name, the move sequence as a flat
list of UCI strings (white_move, black_move, white_move, black_move, ...),
and the Elo range. White's moves are what the bot REQUIRES from the human
to stay in book; black's moves are what the bot plays.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import chess


# Opening probability curve: how often the bot tries to play a book opening
# at a given Elo. Peaks at 0.95 around Elo 800 (where humans most rigidly
# follow opening prep), then tapers down at higher Elo where stronger players
# vary their play more.
_OPENING_PROB_ANCHORS: list[tuple[int, float]] = [
    (100, 0.20),
    (400, 0.60),
    (800, 0.95),
    (1100, 0.80),
    (1319, 0.50),
]


@dataclass(frozen=True)
class Opening:
    name: str
    # Flat list of UCI strings: [white_move_1, black_move_1, white_move_2, ...].
    # Length must be even — every white move has a paired black reply.
    moves: tuple[str, ...]
    # Inclusive Elo range where this opening is plausible. Outside this range,
    # the opening is never selected.
    elo_min: int
    elo_max: int

    def __post_init__(self) -> None:
        if len(self.moves) % 2 != 0:
            raise ValueError(
                f"opening {self.name!r}: moves list must have even length "
                f"(paired white/black), got {len(self.moves)}"
            )
        if self.elo_min > self.elo_max:
            raise ValueError(
                f"opening {self.name!r}: elo_min {self.elo_min} > elo_max {self.elo_max}"
            )


# Note on authoring: white moves listed here are what we REQUIRE from the
# human. If the human plays anything else, the bot abandons the opening on
# its first scripted reply (i.e. before playing) and falls through to normal
# sampled play.
OPENINGS: tuple[Opening, ...] = (
    # ── Mainstream openings (Elo 400+) ──────────────────────────────────
    Opening(
        name="Italian Game (Black)",
        moves=("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5"),
        elo_min=400, elo_max=1319,
    ),
    Opening(
        name="Ruy Lopez (Black)",
        moves=("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"),
        elo_min=500, elo_max=1319,
    ),
    Opening(
        name="Queen's Gambit Declined",
        moves=("d2d4", "d7d5", "c2c4", "e7e6"),
        elo_min=500, elo_max=1319,
    ),
    Opening(
        name="Sicilian Defense (Najdorf)",
        moves=("e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4",
               "f3d4", "g8f6", "b1c3", "a7a6"),
        elo_min=700, elo_max=1319,
    ),
    Opening(
        name="French Defense",
        moves=("e2e4", "e7e6"),
        elo_min=400, elo_max=1319,
    ),
    Opening(
        name="Caro-Kann",
        moves=("e2e4", "c7c6"),
        elo_min=400, elo_max=1319,
    ),
    Opening(
        name="King's Indian Defense",
        moves=("d2d4", "g8f6", "c2c4", "g7g6", "b1c3", "f8g7"),
        elo_min=600, elo_max=1319,
    ),
    Opening(
        name="Nimzo-Indian",
        moves=("d2d4", "g8f6", "c2c4", "e7e6", "b1c3", "f8b4"),
        elo_min=700, elo_max=1319,
    ),

    # ── Aggressive / early-queen attacks (Elo 300-500) ─────────────────
    Opening(
        name="Scandinavian Defense",
        moves=("e2e4", "d7d5", "e4d5", "d8d5"),
        elo_min=300, elo_max=700,
    ),
    Opening(
        name="Englund Gambit (early queen)",
        moves=("d2d4", "e7e5", "d4e5", "b8c6", "g1f3", "d8e7"),
        elo_min=300, elo_max=600,
    ),
    Opening(
        name="Scholar's Mate defense",
        # Bot defends against white's Scholar's Mate attempt.
        moves=("e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g7g6"),
        elo_min=200, elo_max=500,
    ),

    # ── Traps (mid-range) ───────────────────────────────────────────────
    Opening(
        name="Stafford Gambit",
        # Famous trappy line — black sacs a pawn for rapid development.
        moves=("e2e4", "e7e5", "g1f3", "g8f6", "f3e5", "b8c6",
               "e5c6", "d7c6"),
        elo_min=400, elo_max=800,
    ),
    Opening(
        name="Latvian Gambit",
        # Wild and unsound but trap-rich.
        moves=("e2e4", "e7e5", "g1f3", "f7f5"),
        elo_min=300, elo_max=700,
    ),

    # ── Offbeat / joke openings (rare, wide range) ──────────────────────
    Opening(
        name="Hippopotamus",
        moves=("e2e4", "g7g6", "d2d4", "f8g7", "b1c3", "b7b6",
               "g1f3", "d7d6"),
        elo_min=300, elo_max=1100,
    ),
    Opening(
        name="Owen's Defense",
        moves=("e2e4", "b7b6"),
        elo_min=400, elo_max=900,
    ),
    Opening(
        name="Bongcloud (Black)",
        # Joke: black plays Ke7 on move 2. Will be rejected by the top-10
        # check almost immediately, but if it survives one move it's funny.
        moves=("e2e4", "e7e5", "g1f3", "e8e7"),
        elo_min=100, elo_max=600,
    ),
)


def _interp_float(anchors: list[tuple[int, float]], x: int) -> float:
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            progress = (x - x0) / (x1 - x0)
            return y0 + progress * (y1 - y0)
    return anchors[-1][1]


def opening_probability_for_elo(elo: int) -> float:
    """Per-game probability that the bot tries to play a book opening. Peaks
    at 0.95 around Elo 800; tapers off at higher Elo where stronger players
    vary their prep more."""
    return _interp_float(_OPENING_PROB_ANCHORS, elo)


def openings_for_elo(elo: int) -> list[Opening]:
    """Subset of OPENINGS whose Elo range contains `elo`. Empty if none
    apply (shouldn't happen given the curated coverage, but defensive)."""
    return [op for op in OPENINGS if op.elo_min <= elo <= op.elo_max]


def pick_opening_for_elo(elo: int, rng: random.Random | None = None) -> Opening | None:
    """Pick an opening uniformly at random from those in range for `elo`.
    Returns None if no openings apply.

    Caller is responsible for first rolling against opening_probability_for_elo
    to decide whether to attempt an opening at all — this function always
    returns one if any are in range."""
    candidates = openings_for_elo(elo)
    if not candidates:
        return None
    r = rng or random
    return r.choice(candidates)


def book_move_for_position(
    opening: Opening,
    history_ucis: list[str],
) -> chess.Move | None:
    """Given the move history so far (all moves played by both sides, in UCI
    form, oldest first), return the bot's next scripted book move — or None
    to signal "abandon the opening."

    Returns None when:
      - history is longer than the opening's scripted prefix (we've exited
        the book),
      - the human's most recent move doesn't match the white move the
        opening expects at this ply.

    The bot is always black, so opening.moves alternates white, black, white,
    black starting from index 0. After N total plies the bot's reply (if any)
    is opening.moves[N], and that's only valid if all preceding plies match.
    """
    n_plies = len(history_ucis)
    # We need a black reply, which sits at an odd index (1, 3, 5, ...) in the
    # moves list. Before playing it, the history must be at length n_plies
    # where n_plies is odd-and-one-less than that index... i.e. we play
    # opening.moves[n_plies] when n_plies is odd.
    if n_plies % 2 == 0:
        # Bot is asked to play but the history length suggests white just
        # moved an even number of times — that's the human's turn, not ours.
        # Shouldn't happen via the cog (cog calls us when it's black to move).
        return None
    if n_plies >= len(opening.moves):
        # We're past the book.
        return None
    # Validate the preceding history matches the opening's white moves.
    for i, expected in enumerate(opening.moves[:n_plies]):
        if i % 2 == 1:
            # Black moves: those are the bot's prior scripted moves — by
            # definition they match (we played them).
            continue
        if history_ucis[i] != expected:
            # Human deviated from the opening line.
            return None
    return chess.Move.from_uci(opening.moves[n_plies])
