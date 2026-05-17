-- 0024: add `opening` to chess_games so the bot's chosen opening line
-- survives bot restarts. NULL means the bot is not following an opening
-- (either it never started one, or it has been abandoned mid-game).

ALTER TABLE chess_games ADD COLUMN IF NOT EXISTS opening VARCHAR(64) NULL;
