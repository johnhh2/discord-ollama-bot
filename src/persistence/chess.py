import json

from src import state
from src.db import with_cursor


_CHESS_GAME_UPSERT_SQL = (
    "INSERT INTO chess_games "
    "(channel_id, fen, pgn, white_id, black_id, current_id, amount, last_move, "
    "board_msg_id, elo, embed_msg_id, turn_started_at, white_seconds, black_seconds) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "fen=VALUES(fen), pgn=VALUES(pgn), white_id=VALUES(white_id), black_id=VALUES(black_id), "
    "current_id=VALUES(current_id), amount=VALUES(amount), last_move=VALUES(last_move), "
    "board_msg_id=VALUES(board_msg_id), elo=VALUES(elo), "
    "embed_msg_id=VALUES(embed_msg_id), "
    "turn_started_at=VALUES(turn_started_at), "
    "white_seconds=VALUES(white_seconds), "
    "black_seconds=VALUES(black_seconds)"
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
        int(game["elo"]) if game.get("elo") is not None else None,
        int(game["embed_msg_id"]) if game.get("embed_msg_id") is not None else None,
        int(game["turn_started_at"]) if game.get("turn_started_at") is not None else None,
        int(game.get("white_seconds", 0)),
        int(game.get("black_seconds", 0)),
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


_CHESS_USER_STATS_UPSERT_SQL = (
    "INSERT INTO chess_user_stats "
    "(user_id, max_elo_defeated, total_elo_defeated, bonus_bins) "
    "VALUES (%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "max_elo_defeated=VALUES(max_elo_defeated), "
    "total_elo_defeated=VALUES(total_elo_defeated), "
    "bonus_bins=VALUES(bonus_bins)"
)


async def save_chess_user_stats(uid: int) -> None:
    """Mirror state.chess_user_stats[uid] into the chess_user_stats table.
    Takes the whole row in one shot (like save_property_owner) so a call
    site can't silently drop a column."""
    row = state.chess_user_stats.get(str(uid))
    if row is None:
        return
    async with with_cursor() as cur:
        await cur.execute(_CHESS_USER_STATS_UPSERT_SQL, (
            int(uid),
            int(row.get("max_elo_defeated", 0)),
            int(row.get("total_elo_defeated", 0)),
            json.dumps(sorted(int(b) for b in row.get("bonus_bins", ()))),
        ))


async def save_chess_report(
    *, guild_id: int | None, channel_id: int, white_id: int, black_id: int,
    winner_id: int | None, result: str, pgn: str, final_fen: str,
    elo: int | None = None,
) -> int:
    """Persist a finished chess game. `elo` is NULL for PvP games and the
    bot's Elo bin for bot games — used to bucket bot-game records by
    difficulty (via load_bot_head_to_head)."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO chess_reports "
            "(guild_id, channel_id, white_id, black_id, winner_id, result, pgn, final_fen, elo) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                int(guild_id) if guild_id is not None else None,
                int(channel_id),
                int(white_id),
                int(black_id),
                int(winner_id) if winner_id is not None else None,
                result,
                pgn,
                final_fen,
                int(elo) if elo is not None else None,
            ),
        )
        return cur.lastrowid


async def load_head_to_head(uid_a: int, uid_b: int) -> dict:
    """All-time head-to-head between two users from chess_reports.

    Returns {"wins_a": N, "wins_b": N, "draws": N} with totals across every
    guild. NULL winner_id counts as a draw.
    """
    a, b = int(uid_a), int(uid_b)
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT winner_id, COUNT(*) FROM chess_reports "
            "WHERE (white_id=%s AND black_id=%s) OR (white_id=%s AND black_id=%s) "
            "GROUP BY winner_id",
            (a, b, b, a),
        )
        rows = await cur.fetchall()
    wins_a = wins_b = draws = 0
    for winner_id, count in rows:
        if winner_id is None:
            draws += int(count)
        elif int(winner_id) == a:
            wins_a += int(count)
        elif int(winner_id) == b:
            wins_b += int(count)
    return {"wins_a": wins_a, "wins_b": wins_b, "draws": draws}


async def load_bot_head_to_head(uid: int, bot_user_id: int, elo: int) -> dict:
    """All-time record between `uid` and the bot AT THE GIVEN ELO bin.
    Returns {'wins': N, 'losses': N, 'draws': N} where 'wins' = the user
    won, 'losses' = the bot won, 'draws' = neither.

    Only counts games where chess_reports.elo == the given elo, so a
    user's record at Elo 400 doesn't bleed into their record at Elo 700.
    """
    u, b = int(uid), int(bot_user_id)
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT winner_id, COUNT(*) FROM chess_reports "
            "WHERE elo=%s "
            "AND ((white_id=%s AND black_id=%s) OR (white_id=%s AND black_id=%s)) "
            "GROUP BY winner_id",
            (int(elo), u, b, b, u),
        )
        rows = await cur.fetchall()
    wins = losses = draws = 0
    for winner_id, count in rows:
        if winner_id is None:
            draws += int(count)
        elif int(winner_id) == u:
            wins += int(count)
        elif int(winner_id) == b:
            losses += int(count)
    return {"wins": wins, "losses": losses, "draws": draws}


async def count_pvp_wins_in_guild(uid: int, guild_id: int, bot_user_id: int) -> int:
    """How many PvP chess wins this user has in this guild. Bot games are
    excluded by filtering out reports where either side is bot_user_id."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM chess_reports "
            "WHERE guild_id=%s AND winner_id=%s "
            "AND white_id<>%s AND black_id<>%s",
            (int(guild_id), int(uid), int(bot_user_id), int(bot_user_id)),
        )
        row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


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
