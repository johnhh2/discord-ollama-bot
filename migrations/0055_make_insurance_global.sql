-- 0055: make insurance bot-wide instead of per-server.
--
-- Insurance (and its subscription) now protects the holder in EVERY server,
-- like balances and artifacts. Rows keep living in shop_effects but under the
-- sentinel guild_id=0; the PK (guild_id, user_id, effect_type) then enforces
-- one global policy per user. Other effect types stay per-guild.
--
-- Data migration: collapse each user's per-guild insurance rows into one
-- guild_id=0 row carrying their LATEST expiry — nobody's coverage shrinks
-- anywhere they had it, and shorter overlapping policies fold into the
-- longest one. Multi-guild subscriptions collapse to a single sub (charged
-- once per daily claim now, not once per guild). protected_from is reset to
-- the canonical full list.
--
-- Idempotent / retry-safe: guild_id=0 insurance rows could not exist before
-- this migration (purchases always ran inside a guild), and the NOT IN guard
-- skips users whose global row was already written by a half-applied run.

INSERT INTO shop_effects (guild_id, user_id, effect_type, expires_at, history_json)
SELECT 0, user_id, 'insurance', MAX(expires_at),
       '["ragebait", "mock", "nickname", "role", "steal", "tax", "spellcheck"]'
  FROM shop_effects
 WHERE effect_type='insurance' AND guild_id<>0
   AND user_id NOT IN (SELECT user_id FROM shop_effects WHERE effect_type='insurance' AND guild_id=0)
 GROUP BY user_id;

DELETE FROM shop_effects WHERE effect_type='insurance' AND guild_id<>0;

INSERT INTO shop_effects (guild_id, user_id, effect_type)
SELECT 0, user_id, 'insurance_sub'
  FROM shop_effects
 WHERE effect_type='insurance_sub' AND guild_id<>0
   AND user_id NOT IN (SELECT user_id FROM shop_effects WHERE effect_type='insurance_sub' AND guild_id=0)
 GROUP BY user_id;

DELETE FROM shop_effects WHERE effect_type='insurance_sub' AND guild_id<>0;
