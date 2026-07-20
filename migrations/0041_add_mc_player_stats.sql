-- 0041: persistent Minecraft player tracking, powering the daily-players
-- overlay on !graph minecraft.
--
-- The Bedrock pong only carries player *counts* (never names — see the
-- minecraft_cog module docstring), so "joins" and "leaves" are count deltas
-- observed by the ~60s monitor poll. mc_player_events keeps every observed
-- count change; mc_daily_player_stats is the compact per-day rollup the
-- graph reads (peak concurrent players, cumulative joins, accumulated
-- player-seconds). Both are pruned by the monitor at a ~10-year horizon
-- (MC_PLAYER_STATS_RETENTION_DAYS), matching GRAPH_HISTORY_RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS mc_player_events (
    ts BIGINT NOT NULL,
    delta INT NOT NULL,
    players INT NOT NULL,
    PRIMARY KEY (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mc_daily_player_stats (
    stat_date VARCHAR(10) NOT NULL,
    max_concurrent INT NOT NULL DEFAULT 0,
    total_joins INT NOT NULL DEFAULT 0,
    player_seconds DOUBLE NOT NULL DEFAULT 0,
    PRIMARY KEY (stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
