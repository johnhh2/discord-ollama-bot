-- 0026: drop chess_games.opening column. The opening-book system was
-- removed when the chess bot switched to Maia (which plays human-shaped
-- openings naturally — no scripted book needed). Migration 0024 added
-- this column; this migration drops it.

ALTER TABLE chess_games DROP COLUMN IF EXISTS opening;
