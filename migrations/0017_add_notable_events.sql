-- 0017: notable_events — a per-day event log of "things worth a recap quip"
-- that the all-time `records` table can't express.
--
-- The records table only stores each category's all-time best, with no
-- timestamp, so it can't answer "what records were set TODAY" — and a
-- lottery win that isn't a record isn't logged anywhere at all. This table
-- fills that gap: one row per notable moment, tagged with the 5am-CT day
-- string so !recap can pull just today's.
--
-- Written by:
--   • announce_record() in src/helpers.py — every record break (kind='record').
--   • the lottery draw in lottery_cog.py — every lottery win (kind='lottery_win').
-- Read by:
--   • !recap, via load_notable_events_today().
-- Pruned alongside the *_history tables in the daily reset (same cutoff).

CREATE TABLE IF NOT EXISTS notable_events (
    id           BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    guild_id     BIGINT       NOT NULL,
    event_day    VARCHAR(10)  NOT NULL,   -- 5am-CT "day" string, e.g. 2026-05-14
    kind         VARCHAR(24)  NOT NULL,   -- 'record' | 'lottery_win'
    category     VARCHAR(32),             -- record category (NULL for lottery_win)
    holder_name  VARCHAR(64)  NOT NULL,
    value        BIGINT       NOT NULL,
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_notable_events_guild_day
    ON notable_events (guild_id, event_day);
