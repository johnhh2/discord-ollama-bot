-- 0051: insurance subscriptions (`!shop insurance sub` / `unsub`).
--
-- A subscription auto-renews the user's per-guild insurance for 24h at each
-- daily claim, charging SHOP_INSURANCE_COST out of the claim. It persists as
-- a shop_effects row with effect_type='insurance_sub' and NULL expires_at
-- (a subscription has no expiry — it lives until the user unsubscribes),
-- so all this migration does is widen the ENUM like 0030/0032 did.
-- Re-running the MODIFY with the same definition is a harmless no-op.
ALTER TABLE shop_effects
    MODIFY COLUMN effect_type ENUM('ragebait','mock','curse','tax','spellcheck','insurance','insurance_sub') NOT NULL;
