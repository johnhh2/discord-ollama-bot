-- 0042: long-term daily Minecraft ping rollup.
--
-- mc_ping_samples keeps only 7 days of ~60s polls; this table preserves a
-- compact per-CT-day summary for ~10 years (pruned by the monitor at
-- MC_STATS_RETENTION_DAYS). Rows are written by the monitor's hourly
-- rollup only for COMPLETED days — the ongoing day stays in the sample
-- table until it finishes. Downtime polls count as ping 0, matching the
-- !graph minecraft line semantics, so min_ping 0 means the server was
-- offline at some point that day.
CREATE TABLE IF NOT EXISTS mc_daily_ping_stats (
    stat_date VARCHAR(10) NOT NULL,
    avg_ping DOUBLE NOT NULL,
    min_ping DOUBLE NOT NULL,
    max_ping DOUBLE NOT NULL,
    PRIMARY KEY (stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
