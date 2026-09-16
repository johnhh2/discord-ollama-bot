-- 0068: Minecraft server version history.
--
-- One row per version the monitor has seen the Bedrock server report (the
-- first pong after this migration, then every change). The monitor posts a
-- "server updated" alert when a pong's version differs from the last row,
-- and reads the latest row at boot as its baseline — a server update is a
-- restart, and when the whole stack restarts together the bot would
-- otherwise come back with no version to compare against. A handful of
-- rows a year; never pruned.
CREATE TABLE IF NOT EXISTS mc_server_versions (
    ts BIGINT NOT NULL,
    version VARCHAR(64) NOT NULL,
    PRIMARY KEY (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
