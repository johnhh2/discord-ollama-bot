-- Post-game engine analysis on finished chess games: per-player accuracy
-- stats (ACPL, top-move match rate, estimated performance Elo) as JSON, and
-- the cheat-flag suspect user id for admin review. Both NULL for unanalyzed
-- games (pre-feature history, engine unavailable, or game too short).
ALTER TABLE chess_reports ADD COLUMN IF NOT EXISTS analysis_json TEXT NULL;
ALTER TABLE chess_reports ADD COLUMN IF NOT EXISTS flag_user_id BIGINT UNSIGNED NULL;
