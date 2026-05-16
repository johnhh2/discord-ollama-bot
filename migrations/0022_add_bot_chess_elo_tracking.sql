-- 0022: per-user daily-reset highwater for Stockfish bot defeats.
--
-- When a user beats the chess bot, they get paid 20 coins per NEW elo point
-- beyond their highest bot-elo-defeated TODAY. So beating a 400-elo bot,
-- then a 500-elo bot on the same day, pays (400)*20 + (500-400)*20 = 10000.
-- The next day's 5am CT reset zeroes the highwater, and the same 400 + 500
-- sequence pays out fully again.
--
-- bot_chess_elo_max_date is the 5am-CT day string. The loader treats a row
-- whose date != today as if max_today=0, so we don't need to depend on the
-- daily-reset task ever firing — stale rows self-heal on read.

ALTER TABLE economy_users
    ADD COLUMN IF NOT EXISTS bot_chess_elo_max_today INT UNSIGNED NOT NULL DEFAULT 0;

ALTER TABLE economy_users
    ADD COLUMN IF NOT EXISTS bot_chess_elo_max_date VARCHAR(10) NULL;
