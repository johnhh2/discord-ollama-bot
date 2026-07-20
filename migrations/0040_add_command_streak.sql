-- Sequential-day command-usage streak: bumped once per CT day (5am rollover)
-- the first time a user successfully runs any command. One row per user,
-- global (not guild-scoped), mirroring gambler_streak.
CREATE TABLE IF NOT EXISTS command_streak (
    user_id      BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    last_date    VARCHAR(10)     NOT NULL,
    streak_count INT             NOT NULL DEFAULT 1
);
