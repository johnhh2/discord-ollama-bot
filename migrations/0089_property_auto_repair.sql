-- !assets autorepair, and what the daily claim reports about property theft.
-- The *_since_claim tallies are the owner's: thefts on their deeds,
-- investigator call-outs and arrests, and coins auto-repair spent between
-- claims. The claim reads and zeroes them.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS auto_repair TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS robbed_since_claim INT NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS investigations_since_claim INT NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS arrests_since_claim INT NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS auto_repaired_since_claim BIGINT NOT NULL DEFAULT 0;
