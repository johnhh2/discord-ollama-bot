-- 0045: per-user scratchoff winnings for the current gameplay-day (5am CT
-- rollover), mirroring the scratch_used/scratch_date pattern. Feeds the
-- "best scratchoff day" record — the combined payout of every card a user
-- scratches in one day, which is what !scratches / the dailies 🎟️ button
-- produce in a single batch.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS scratch_won_today BIGINT NOT NULL DEFAULT 0;
