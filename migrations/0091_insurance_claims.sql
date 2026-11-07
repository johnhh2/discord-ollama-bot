-- 0091: per-user log of recent insurance refunds, for the claims surcharge
-- on premiums. JSON {type: {CT day: refunded}}, pruned to
-- INSURANCE_CLAIMS_DAYS on read.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS insurance_claims TEXT NULL;
