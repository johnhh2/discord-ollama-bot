-- 0034: Discord gateway heartbeat latency snapshot for `!graph ping`.
-- NULL means "not measured" (rows written before this column existed, or
-- snapshots taken before the first heartbeat ack after a reconnect).
ALTER TABLE bot_stats_history ADD COLUMN IF NOT EXISTS ping_ms FLOAT NULL;
