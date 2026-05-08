ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS crime_eligible BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill the wallet portion of the threshold. The savings JSON column is
-- not introspectable in pure SQL, so users whose savings push them over 100k
-- (without their wallet alone qualifying) will get latched on their next
-- add_balance / add_savings write. Level-based eligibility is latched by
-- the level-up path in src/leveling.py.
UPDATE economy_users SET crime_eligible = TRUE WHERE balance > 100000;