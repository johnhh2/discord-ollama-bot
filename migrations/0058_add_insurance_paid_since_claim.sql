-- Insurance-premium accrual counters, reported (and reset) by the next daily
-- claim: how much the 5am sweep charged, and how many renewals lapsed
-- unpaid, since the user's last claim.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS ins_paid_since_claim BIGINT NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS ins_lapsed_since_claim INT NOT NULL DEFAULT 0;
