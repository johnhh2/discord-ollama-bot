-- 0030: add the 'spellcheck' effect to shop_effects.effect_type so the
-- `!shop spellcheck @user` purchase can persist. Spellcheck is a time-based
-- effect like 'tax': the AI corrects the target's non-command messages for a
-- purchased number of days. It reuses the existing master_id (purchaser),
-- channel_id (purchase channel, unused for gating), remaining (days bought),
-- and activated_at (start time) columns. Re-running this MODIFY with the same
-- definition is a harmless no-op, so the migration stays idempotent-on-retry.
ALTER TABLE shop_effects
    MODIFY COLUMN effect_type ENUM('ragebait','mock','curse','tax','spellcheck') NOT NULL;
