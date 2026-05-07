-- 0005: per-(guild,user) permission tier overrides used by !setperm.
--
-- Each row promotes a specific user inside a specific guild to act as if
-- they held the named tier when permission checks run. tier is one of
-- 'user', 'server_admin', 'bot_admin'; a row with tier='user' is
-- equivalent to no override and is never written (the command deletes
-- the row instead). The (guild_id, user_id) pair is the PK so a user
-- has at most one override per guild.

CREATE TABLE IF NOT EXISTS user_perm_overrides (
    guild_id BIGINT      NOT NULL,
    user_id  BIGINT      NOT NULL,
    tier     VARCHAR(20) NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
