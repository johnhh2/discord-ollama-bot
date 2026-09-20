-- 0069: !count / !counter. Everything is keyed by guild_id — two servers
-- with an `afk` counter share nothing but the name.
CREATE TABLE IF NOT EXISTS counters (
    guild_id      BIGINT UNSIGNED NOT NULL,
    name          VARCHAR(32)     NOT NULL,
    description   VARCHAR(255)    NOT NULL,
    user_required TINYINT(1)      NOT NULL DEFAULT 0,
    kind          VARCHAR(8)      NOT NULL DEFAULT 'number',
    created_by    BIGINT UNSIGNED NOT NULL,
    created_at    BIGINT          NOT NULL,
    PRIMARY KEY (guild_id, name)
);

-- user_id 0 is the unattributed bucket (`!count afk 1` with no user). A
-- time counter's value is in seconds.
CREATE TABLE IF NOT EXISTS counter_values (
    guild_id BIGINT UNSIGNED NOT NULL,
    name     VARCHAR(32)     NOT NULL,
    user_id  BIGINT UNSIGNED NOT NULL,
    value    BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, name, user_id)
);

-- Users a server/bot admin has trusted to write to the guild's counters
-- (`!counter addperm`). Admins themselves never need a row.
CREATE TABLE IF NOT EXISTS counter_perms (
    guild_id   BIGINT UNSIGNED NOT NULL,
    user_id    BIGINT UNSIGNED NOT NULL,
    granted_by BIGINT UNSIGNED NOT NULL,
    granted_at BIGINT          NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
