-- 0028: track per-player thinking time in chess games. turn_started_at is
-- the UNIX timestamp (seconds since epoch) when the current player's turn
-- began; white_seconds and black_seconds are cumulative seconds spent
-- thinking. On each move the elapsed time since turn_started_at is added
-- to whichever player just moved, then turn_started_at resets.

ALTER TABLE chess_games ADD COLUMN IF NOT EXISTS turn_started_at BIGINT UNSIGNED NULL;
ALTER TABLE chess_games ADD COLUMN IF NOT EXISTS white_seconds INT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE chess_games ADD COLUMN IF NOT EXISTS black_seconds INT UNSIGNED NOT NULL DEFAULT 0;
