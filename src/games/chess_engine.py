from __future__ import annotations

import chess


STARTING_FEN = chess.STARTING_FEN


def new_board() -> chess.Board:
    return chess.Board()


def board_from_fen(fen: str) -> chess.Board:
    return chess.Board(fen=fen)


def try_move(board: chess.Board, move_str: str) -> tuple[chess.Move | None, str | None]:
    s = (move_str or "").strip()
    if not s:
        return None, "No move given."

    try:
        return board.parse_san(s), None
    except chess.AmbiguousMoveError:
        return None, f"`{s}` is ambiguous — multiple pieces can make that move. Disambiguate (e.g. `Nbd2`) or use UCI (e.g. `b1d2`)."
    except chess.IllegalMoveError:
        # SAN-shaped but illegal — don't fall through to UCI; that would re-report with a worse message.
        return None, f"`{s}` isn't a legal move here."
    except (chess.InvalidMoveError, ValueError):
        pass

    try:
        move = chess.Move.from_uci(s.lower())
    except (chess.InvalidMoveError, ValueError):
        return None, f"Couldn't parse `{s}`. Use SAN (e.g. `Nf3`) or UCI (e.g. `g1f3`)."

    if move not in board.legal_moves:
        return None, f"`{s}` isn't a legal move here."
    return move, None


def push_with_san(board: chess.Board, move: chess.Move) -> str:
    # SAN is position-dependent (ambiguity); compute before push.
    san = board.san(move)
    board.push(move)
    return san


def describe_capture(board: chess.Board, move: chess.Move) -> str | None:
    """If `move` is a capture against `board` (pre-push), return a phrase like
    "captured Black's knight". Returns None for non-captures.

    Must be called BEFORE the move is pushed — for en passant the captured
    pawn isn't on move.to_square."""
    if not board.is_capture(move):
        return None
    if board.is_en_passant(move):
        # En passant: captured pawn sits on the file of move.to_square at the
        # rank of move.from_square (the pawn that just double-stepped).
        cap_square = chess.square(
            chess.square_file(move.to_square),
            chess.square_rank(move.from_square),
        )
        piece = board.piece_at(cap_square)
    else:
        piece = board.piece_at(move.to_square)
    if piece is None:
        return None
    color_word = "White's" if piece.color == chess.WHITE else "Black's"
    return f"captured {color_word} {chess.piece_name(piece.piece_type)}"


def game_over_info(board: chess.Board) -> tuple[str | None, str | None]:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None, None
    reason = {
        chess.Termination.CHECKMATE: "checkmate",
        chess.Termination.STALEMATE: "stalemate",
        chess.Termination.INSUFFICIENT_MATERIAL: "insufficient material",
        chess.Termination.SEVENTYFIVE_MOVES: "seventy-five-move rule",
        chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition",
        chess.Termination.FIFTY_MOVES: "fifty-move rule",
        chess.Termination.THREEFOLD_REPETITION: "threefold repetition",
        chess.Termination.VARIANT_WIN: "variant win",
        chess.Termination.VARIANT_LOSS: "variant loss",
        chess.Termination.VARIANT_DRAW: "variant draw",
    }.get(outcome.termination, "game over")
    return outcome.result(), reason


def winner_color(board: chess.Board) -> bool | None:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None
    return outcome.winner
