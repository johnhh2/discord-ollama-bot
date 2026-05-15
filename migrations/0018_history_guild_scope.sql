-- 0018: add `guild_id` to crime_history and gambling_history so per-user
-- crime/gambling P/L is scoped per server, matching levelup_history.
--
-- Why: these two tables keyed on (snapshot_date, bucket, user_id) with no
-- guild — so a user's totals were summed across every server they play
-- in. !recap (and the !graph crime/gambling charts) then attributed one
-- server's activity to another. levelup_history already carries guild_id;
-- this brings the other two activity tables in line.
--
-- Existing rows have no guild — backfill to 0, a sentinel "unknown guild".
-- Pre-migration rows therefore never match a real guild_id in !recap or
-- !graph, which is correct: we genuinely don't know where they happened,
-- and they age out within the 14-day pruning window anyway.
--
-- Same three-step shape as 0004 (add column, drop PK, add PK) so the
-- SQLite test translator can handle it — it drops the DROP PRIMARY KEY
-- line and rebuilds the table on ADD PRIMARY KEY.

ALTER TABLE crime_history ADD COLUMN guild_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE crime_history DROP PRIMARY KEY;
ALTER TABLE crime_history ADD PRIMARY KEY (snapshot_date, bucket, guild_id, user_id);

ALTER TABLE gambling_history ADD COLUMN guild_id BIGINT NOT NULL DEFAULT 0;
ALTER TABLE gambling_history DROP PRIMARY KEY;
ALTER TABLE gambling_history ADD PRIMARY KEY (snapshot_date, bucket, guild_id, user_id);
