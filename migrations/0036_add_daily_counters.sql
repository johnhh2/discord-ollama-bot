-- Generic per-gameplay-day counters, one row per (day, counter).
-- `day` is the 5am-CT rollover string from src.economy._ct_today(), so
-- counters reset with the rest of the daily economy state. First user:
-- the "lottery tickets sold today" presence status line, which previously
-- lived only in memory and restarted at 0 on every deploy.
CREATE TABLE IF NOT EXISTS daily_counters (
    day VARCHAR(10) NOT NULL,
    counter VARCHAR(64) NOT NULL,
    value BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, counter)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
