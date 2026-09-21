-- 0070: !idle (src/cogs/idle_cog.py). Characters are per guild — one user
-- may hold one in each server, and nothing is shared between them.
-- Exactly one of next_level_at / remaining is set: a running character has
-- an absolute level-up time, a paused one (offline past the grace window, or
-- the game switched off) has the seconds it still owed when it froze.
CREATE TABLE IF NOT EXISTS idle_characters (
    guild_id         BIGINT UNSIGNED NOT NULL,
    user_id          BIGINT UNSIGNED NOT NULL,
    class_name       VARCHAR(32)     NOT NULL,
    level            INT             NOT NULL DEFAULT 0,
    next_level_at    BIGINT          NULL,
    remaining        BIGINT          NULL,
    law              VARCHAR(8)      NOT NULL DEFAULT 'neutral',
    moral            VARCHAR(8)      NOT NULL DEFAULT 'neutral',
    prestige         INT             NOT NULL DEFAULT 0,
    penalty_total    BIGINT          NOT NULL DEFAULT 0,
    last_seen        BIGINT          NOT NULL,
    last_penalty_at  BIGINT          NOT NULL DEFAULT 0,
    thread_id        BIGINT UNSIGNED NULL,
    created_at       BIGINT          NOT NULL,
    align_changed_at BIGINT          NOT NULL DEFAULT 0,
    duel_day         VARCHAR(10)     NULL,
    items_json       TEXT            NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- One row per guild. ends_at is NULL while no quest is running; not_before
-- is the earliest the next one may start.
CREATE TABLE IF NOT EXISTS idle_quests (
    guild_id     BIGINT UNSIGNED NOT NULL,
    members_json TEXT            NOT NULL,
    description  VARCHAR(255)    NOT NULL DEFAULT '',
    ends_at      BIGINT          NULL,
    not_before   BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id)
);
