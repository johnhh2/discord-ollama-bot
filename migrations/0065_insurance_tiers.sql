-- 0065: insurance tiers + crime refunds.
--
-- Insurance no longer blocks steal/mug/bankheist; it refunds a share of the
-- victim's loss instead, minted by the "insurance company". Three tiers
-- (src/config.py SHOP_INSURANCE_TIERS): basic 50% for 1k/day (100k cap),
-- standard 75% for 3k/day (200k cap), premium 100% for 6k/day (400k cap).
-- The cap applies per incident, so the refund needs no running tally --
-- the policy's tier is the only new state. Every existing policy and
-- subscription was bought at 1k/day, so they all land on the basic tier --
-- nobody's price changes under them.
--
--   shop_effects.insurance_tier   the policy's tier ('insurance' rows) and
--                                 the tier the 5am sweep renews at
--                                 ('insurance_sub' rows)
ALTER TABLE shop_effects ADD COLUMN IF NOT EXISTS insurance_tier VARCHAR(16) NULL;

UPDATE shop_effects
   SET insurance_tier = 'basic'
 WHERE effect_type IN ('insurance', 'insurance_sub')
   AND insurance_tier IS NULL;
