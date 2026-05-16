from src import state
from src.db import with_cursor


_CHESS_GAME_UPSERT_SQL = (
    "INSERT INTO chess_games "
    "(channel_id, fen, pgn, white_id, black_id, current_id, amount, last_move, board_msg_id) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "fen=VALUES(fen), pgn=VALUES(pgn), white_id=VALUES(white_id), black_id=VALUES(black_id), "
    "current_id=VALUES(current_id), amount=VALUES(amount), last_move=VALUES(last_move), "
    "board_msg_id=VALUES(board_msg_id)"
)


def _row_for_game(channel_id: int, game: dict) -> tuple:
    return (
        int(channel_id),
        game["fen"],
        game.get("pgn", ""),
        int(game["white_id"]),
        int(game["black_id"]),
        int(game["current_id"]),
        int(game.get("amount", 0)),
        game.get("last_move", ""),
        int(game["board_msg_id"]) if game.get("board_msg_id") is not None else None,
    )


async def save_chess_game(channel_id: int) -> None:
    game = state.active_chess_games.get(channel_id)
    if game is None:
        async with with_cursor() as cur:
            await cur.execute("DELETE FROM chess_games WHERE channel_id=%s", (int(channel_id),))
        return
    async with with_cursor() as cur:
        await cur.execute(_CHESS_GAME_UPSERT_SQL, _row_for_game(channel_id, game))


async def delete_chess_game(channel_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM chess_games WHERE channel_id=%s", (int(channel_id),))


# Kept for compatibility with !stop in ai_cog.py which iterates and rewrites all chess state.
# Walks state and per-row upserts; the underlying table no longer gets wiped.
async def save_chess_games() -> None:
    channel_ids = list(state.active_chess_games.keys())
    if not channel_ids:
        return
    async with with_cursor() as cur:
        for ch_id in channel_ids:
            game = state.active_chess_games[ch_id]
            await cur.execute(_CHESS_GAME_UPSERT_SQL, _row_for_game(ch_id, game))


async def save_chess_report(
    *, guild_id: int | None, channel_id: int, white_id: int, black_id: int,
    winner_id: int | None, result: str, pgn: str, final_fen: str,
) -> int:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO chess_reports "
            "(guild_id, channel_id, white_id, black_id, winner_id, result, pgn, final_fen) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                int(guild_id) if guild_id is not None else None,
                int(channel_id),
                int(white_id),
                int(black_id),
                int(winner_id) if winner_id is not None else None,
                result,
                pgn,
                final_fen,
            ),
        )
        return cur.lastrowid


async def load_chess_report(report_id: int) -> dict | None:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT report_id, guild_id, channel_id, white_id, black_id, winner_id, "
            "result, pgn, final_fen, finished_at "
            "FROM chess_reports WHERE report_id=%s",
            (int(report_id),),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "report_id": row[0],
        "guild_id": row[1],
        "channel_id": row[2],
        "white_id": row[3],
        "black_id": row[4],
        "winner_id": row[5],
        "result": row[6],
        "pgn": row[7],
        "final_fen": row[8],
        "finished_at": row[9],
    }
