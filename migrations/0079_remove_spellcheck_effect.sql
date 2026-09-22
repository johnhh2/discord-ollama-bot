-- 0079: drop the 'spellcheck' shop effect (`!shop spellcheck @user [days]`),
-- removed from the bot. Delete before the MODIFY: MariaDB rejects narrowing an
-- ENUM while rows still hold the value being removed.
DELETE FROM shop_effects WHERE effect_type='spellcheck';

ALTER TABLE shop_effects
    MODIFY COLUMN effect_type ENUM('ragebait','mock','curse','tax','insurance','insurance_sub') NOT NULL;
