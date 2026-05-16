-- 0020: chess overhaul — replace JSON-blob chess_games with a column-per-field
-- schema, and add chess_reports for completed games.
--
-- Old chess_games stored the board as a JSON nested-list of Unicode glyphs plus
-- a list of UCI move strings. The new shape stores FEN (canonical board state,
-- restored on load with chess.Board(fen=...)) and PGN (full move history with
-- SAN). Active in-progress games at the time of this migration are dropped — no
-- reliable way to derive correct castling rights / en passant square from the
-- old board, and game count is small.
--
-- chess_reports archives finished games so `!chess view <id>` can replay them.
-- Active games (chess_games) are deleted on completion; only completed games
-- live in chess_reports.
--
-- Written by:
--   • cmd_chess / cmd_move_chess in src/games/chess.py — every move upserts
--     chess_games via save_chess_game(channel_id); game-end inserts a
--     chess_reports row and deletes the chess_games row.
-- Read by:
--   • init_db_state on boot, hydrating state.active_chess_games from the new
--     columns.
--   • !chess view <id>, via load_chess_report(report_id).

DROP TABLE IF EXISTS chess_games;

CREATE TABLE IF NOT EXISTS chess_games (
    channel_id    BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    fen           TEXT            NOT NULL,
    pgn           MEDIUMTEXT      NOT NULL,
    white_id      BIGINT UNSIGNED NOT NULL,
    black_id      BIGINT UNSIGNED NOT NULL,
    current_id    BIGINT UNSIGNED NOT NULL,
    amount        BIGINT          NOT NULL DEFAULT 0,
    last_move     VARCHAR(255)    NOT NULL DEFAULT '',
    board_msg_id  BIGINT UNSIGNED NULL,
    started_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chess_reports (
    report_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    guild_id     BIGINT UNSIGNED NULL,
    channel_id   BIGINT UNSIGNED NOT NULL,
    white_id     BIGINT UNSIGNED NOT NULL,
    black_id     BIGINT UNSIGNED NOT NULL,
    winner_id    BIGINT UNSIGNED NULL,        -- NULL for draws
    result       VARCHAR(8)      NOT NULL,    -- '1-0' | '0-1' | '1/2-1/2'
    pgn          MEDIUMTEXT      NOT NULL,
    final_fen    TEXT            NOT NULL,
    finished_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_chess_reports_white
    ON chess_reports (white_id);
CREATE INDEX IF NOT EXISTS idx_chess_reports_black
    ON chess_reports (black_id);
CREATE INDEX IF NOT EXISTS idx_chess_reports_finished
    ON chess_reports (finished_at);
