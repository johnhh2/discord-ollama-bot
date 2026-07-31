-- 0043: per-user daily lottery ticket discount. The first 10 tickets a user
-- buys each gameplay-day (5am CT rollover) cost half price; these columns
-- track how many discounted tickets were used and for which day, mirroring
-- the scratch_used/scratch_date pattern.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS lottery_disc_used INT NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS lottery_disc_date VARCHAR(10) NULL;
