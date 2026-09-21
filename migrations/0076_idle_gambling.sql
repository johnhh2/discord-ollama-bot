-- 0076: !idle town gambling. gamble_town / gamble_visit_at / gamble_budget
-- describe the current town visit: the gold still allowed on the tables
-- (a fifth of the purse the character arrived with). The rest are lifetime
-- tallies for the sheet.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gamble_town VARCHAR(32) NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gamble_visit_at BIGINT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gamble_budget BIGINT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gambles INT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gamble_won BIGINT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS gamble_lost BIGINT NOT NULL DEFAULT 0;
