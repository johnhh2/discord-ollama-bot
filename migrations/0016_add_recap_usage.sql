-- 0016: recap_usage — per-user, per-guild daily-cap tracking for !recap.
--
-- !recap is `everyone` tier but limited to one run per user per guild per
-- 5am-CT "day". This table stores the last day-string a user ran it in;
-- the command compares it against `_ct_today()` and refuses if they match.
-- One row per (guild_id, user_id); the day string is overwritten on each
-- successful run, so the table never grows beyond the active user set.

CREATE TABLE IF NOT EXISTS recap_usage (
    guild_id   BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    last_date  VARCHAR(10) NOT NULL,
    PRIMARY KEY (guild_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
