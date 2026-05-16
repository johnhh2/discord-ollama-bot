-- 0021: add `elo` to chess_games so bot-vs-human games resume at the right
-- strength after a bot restart. NULL for PvP games (no engine involved).

ALTER TABLE chess_games ADD COLUMN IF NOT EXISTS elo INT UNSIGNED NULL;
