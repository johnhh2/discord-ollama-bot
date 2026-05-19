-- 0027: add `elo` to chess_reports so bot-game records can be bucketed by
-- the Elo at which the bot was playing. NULL = PvP game (no engine).
-- Used by load_bot_head_to_head to compute per-Elo win/loss records
-- between a human and the bot.

ALTER TABLE chess_reports ADD COLUMN IF NOT EXISTS elo INT UNSIGNED NULL;
