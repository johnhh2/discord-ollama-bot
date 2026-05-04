-- 0004: widen the 6 history tables with a `bucket` column so each calendar
-- day holds up to 4 data points (one per 6h CT window: 0=00-06, 1=06-12,
-- 2=12-18, 3=18-24). Existing rows backfill to bucket=0; the next 14 days
-- of writes will fill in the new buckets, and after the 14-day pruning
-- window all old daily rows naturally age out.
--
-- Each table needs three steps: add the column, drop the old PK, add the
-- new PK including bucket. Steps are split into separate statements so the
-- SQLite test translator (which doesn't support multi-action ALTER or PK
-- changes) can drop the PK statements and let SQLite use a permissive PK
-- — tests track PKs in tests/fakes/db.py:_TABLE_PKS for ON CONFLICT
-- translation, not via the SQLite engine's constraint.

ALTER TABLE balance_history ADD COLUMN bucket TINYINT NOT NULL DEFAULT 0;
ALTER TABLE balance_history DROP PRIMARY KEY;
ALTER TABLE balance_history ADD PRIMARY KEY (snapshot_date, bucket, user_id);

ALTER TABLE bot_stats_history ADD COLUMN bucket TINYINT NOT NULL DEFAULT 0;
ALTER TABLE bot_stats_history DROP PRIMARY KEY;
ALTER TABLE bot_stats_history ADD PRIMARY KEY (snapshot_date, bucket);

ALTER TABLE bot_command_usage_history ADD COLUMN bucket TINYINT NOT NULL DEFAULT 0;
ALTER TABLE bot_command_usage_history DROP PRIMARY KEY;
ALTER TABLE bot_command_usage_history ADD PRIMARY KEY (snapshot_date, bucket, cog_name);

ALTER TABLE crime_history ADD COLUMN bucket TINYINT NOT NULL DEFAULT 0;
ALTER TABLE crime_history DROP PRIMARY KEY;
ALTER TABLE crime_history ADD PRIMARY KEY (snapshot_date, bucket, user_id);

ALTER TABLE gambling_history ADD COLUMN bucket TINYINT NOT NULL DEFAULT 0;
ALTER TABLE gambling_history DROP PRIMARY KEY;
ALTER TABLE gambling_history ADD PRIMARY KEY (snapshot_date, bucket, user_id);

ALTER TABLE levelup_history ADD COLUMN bucket TINYINT NOT NULL DEFAULT 0;
ALTER TABLE levelup_history DROP PRIMARY KEY;
ALTER TABLE levelup_history ADD PRIMARY KEY (snapshot_date, bucket, guild_id, user_id);
