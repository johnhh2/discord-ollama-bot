-- 0029: per-subscriber ignore list for `!subscribe ignore <user>`. When the
-- member who triggers a voice channel's 0→1 human transition is in a
-- subscriber's ignore list, that subscriber is skipped WITHOUT consuming their
-- per-(channel,user) cooldown — a later, non-ignored trigger still pings.
--
-- Scope is per (guild, subscriber, ignored_user): ignoring someone suppresses
-- their pings across every channel you subscribe to in that guild, and only
-- for you. PK is (guild_id, user_id, ignored_user_id).

CREATE TABLE IF NOT EXISTS voice_ping_ignores (
    guild_id          BIGINT NOT NULL,
    user_id           BIGINT NOT NULL,
    ignored_user_id   BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id, ignored_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
