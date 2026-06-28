-- 0031: scope shop effects per-server and fold insurance into shop_effects.
--
-- Before this, shop_effects was keyed (user_id, effect_type) with no guild —
-- so a user could hold at most one of each effect across ALL servers, and an
-- effect bought in server A would fire in server B. Insurance lived in a
-- separate, also-global shop_insurance table. This migration:
--
--   1. adds guild_id to shop_effects and re-keys the PK to
--      (guild_id, user_id, effect_type) so each (guild, user) has its own set;
--   2. adds an expires_at column (used by time-based effects: insurance, and
--      future unification of tax/spellcheck) and widens the effect_type ENUM
--      to include 'insurance';
--   3. drops the old global shop_insurance table.
--
-- Existing rows in BOTH tables are dropped: they carry no guild_id and can't be
-- safely assigned to a server, and insurance/effects are short-lived anyway.
-- Users re-buy going forward; everything is per-server from here.
--
-- Three-step add/drop-PK/add-PK shape (like 0004/0018) so the SQLite test
-- translator can handle it. The MODIFY COLUMN that widens the ENUM is a no-op
-- in tests (ENUM is TEXT there) and the translator drops it.

DELETE FROM shop_effects;

ALTER TABLE shop_effects ADD COLUMN IF NOT EXISTS guild_id BIGINT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE shop_effects ADD COLUMN IF NOT EXISTS expires_at DOUBLE NULL;

ALTER TABLE shop_effects
    MODIFY COLUMN effect_type ENUM('ragebait','mock','curse','tax','spellcheck','insurance') NOT NULL;

ALTER TABLE shop_effects DROP PRIMARY KEY;
ALTER TABLE shop_effects ADD PRIMARY KEY (guild_id, user_id, effect_type);

DROP TABLE IF EXISTS shop_insurance;
