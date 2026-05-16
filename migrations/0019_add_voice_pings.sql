-- 0019: subscriptions for `!ping <voice_channel>` — DM the subscriber when a
-- voice channel goes from 0 humans to 1+. PK is (channel_id, user_id) so a
-- user can subscribe to many channels and a channel can have many subscribers.
--
-- last_pinged_at is a unix timestamp (seconds). It enforces the 30-minute
-- per-(channel,user) cooldown so a channel that's repeatedly emptying and
-- refilling doesn't spam the subscriber. NULL means "never pinged yet".
--
-- guild_id is stored alongside for guild-scoped listing in `!ping` with no
-- args — Discord channels carry a guild_id but we'd otherwise have to fetch
-- each channel just to filter the list.

CREATE TABLE IF NOT EXISTS voice_pings (
    guild_id        BIGINT NOT NULL,
    channel_id      BIGINT NOT NULL,
    user_id         BIGINT NOT NULL,
    last_pinged_at  BIGINT,
    PRIMARY KEY (channel_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_voice_pings_user ON voice_pings (user_id);
