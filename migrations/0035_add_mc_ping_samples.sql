-- Per-poll Minecraft monitor samples (one row per ~60s status ping).
-- Powers the !mc uptime/avg-ping stats across bot restarts and the
-- fine-grained last-7-days tail of !graph minecraft. The monitor loop
-- prunes rows older than its 7-day window, so the table stays ~10k rows.
CREATE TABLE IF NOT EXISTS mc_ping_samples (
    ts BIGINT NOT NULL,
    online TINYINT(1) NOT NULL,
    latency_ms DOUBLE NULL,
    PRIMARY KEY (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
