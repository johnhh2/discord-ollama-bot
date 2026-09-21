-- 0073: !idle gold — the game's own currency, per character like everything
-- else here and unrelated to economy_users. rush_day / extra_duel_day are
-- the gameplay-day (5am CT) of the last once-a-day shop purchase. traded_at
-- is the last automatic town errand; auto_trade is the player's opt-out.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gold BIGINT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS rush_day VARCHAR(10) NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS extra_duel_day VARCHAR(10) NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS auto_trade TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS traded_at BIGINT NOT NULL DEFAULT 0;
