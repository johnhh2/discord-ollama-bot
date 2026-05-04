-- 0002: per-cog daily command-usage snapshot, powering !graph commands.

CREATE TABLE IF NOT EXISTS bot_command_usage_history (
    snapshot_date VARCHAR(10) NOT NULL,
    cog_name      VARCHAR(64) NOT NULL,
    count         INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, cog_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
