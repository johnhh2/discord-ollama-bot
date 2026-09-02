-- Equipped chess cosmetics (!chess <name>): one row per user, global like
-- the chess_unlocks they come from. Values are renderer keys
-- (chess_render.PIECE_SET_KEYS / BOARD_THEMES); the renderer falls back to
-- its defaults on an unknown value, so a stale row can never break board
-- rendering.
CREATE TABLE IF NOT EXISTS chess_equipped (
    user_id BIGINT NOT NULL PRIMARY KEY,
    piece_set VARCHAR(32) NOT NULL DEFAULT 'cburnett',
    board_theme VARCHAR(32) NOT NULL DEFAULT 'default'
);
