-- 0075: !idle monster encounters — lifetime tallies for the sheet and ladder.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS mob_kills INT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS mob_deaths INT NOT NULL DEFAULT 0;
