-- Per-user dailies-stake preference. When FALSE (the default) the dailies
-- 🪙/🎰 reactions leave property revenue out of the flip/slots stake — it
-- still banks with the claim. Users opt in with `!daily property`.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS daily_gamble_property BOOLEAN NOT NULL DEFAULT FALSE;
