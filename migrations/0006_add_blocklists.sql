-- 0006: bot-side blocklists used by !ban / !unban (per-guild) and
-- !globalban / !globalunban (bot-wide).
--
-- A row in blocklist silences a user inside a single guild — the bot
-- ignores their messages there (no AI reply, no command processing, no
-- XP/economy/tax/curse side effects). global_blocklist applies the same
-- silence everywhere. Discord membership is untouched; humans can still
-- talk to a banned user. server_admins and bot_admins are excluded from
-- being banned at the command layer (see is_bannable).

CREATE TABLE IF NOT EXISTS blocklist (
    guild_id   BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    reason     TEXT,
    banned_by  BIGINT      NOT NULL,
    banned_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS global_blocklist (
    user_id    BIGINT      NOT NULL,
    reason     TEXT,
    banned_by  BIGINT      NOT NULL,
    banned_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id)
);
