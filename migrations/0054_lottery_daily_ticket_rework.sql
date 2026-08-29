-- 0054: lottery ticket rework. Bulk buying, the half-price discount, and
-- automatch are gone. Tickets now come from exactly three places:
--   * one 1,000-coin daily ticket per user per server (dailies 🎟️ button, or
--     a confirm prompt on !lottery when today's ticket is still unbought)
--   * one free ticket per week for beating a 500+ Elo chess bot
--   * one more free ticket per week if the win was at 1100+ Elo
-- lottery_ticket_grants tracks the gates: daily_day is the gameplay-day of
-- the user's last daily-ticket purchase in that guild; chess_week_500 /
-- chess_week_1100 hold the ISO week of the last free chess ticket claimed in
-- that guild. Mirrored into state.lottery_ticket_grants at boot.
CREATE TABLE IF NOT EXISTS lottery_ticket_grants (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    daily_day VARCHAR(10) NULL,
    chess_week_500 VARCHAR(10) NULL,
    chess_week_1100 VARCHAR(10) NULL,
    PRIMARY KEY (guild_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Automatch existed to keep up with bulk buyers; with everyone capped at one
-- purchased ticket a day it has nothing to match. Feature removed.
DROP TABLE IF EXISTS lottery_automatch;

-- Half-price discount counters (migration 0043) are dead with the discount.
ALTER TABLE economy_users DROP COLUMN IF EXISTS lottery_disc_used;
ALTER TABLE economy_users DROP COLUMN IF EXISTS lottery_disc_date;
