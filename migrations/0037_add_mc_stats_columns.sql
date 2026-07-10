-- 0037: persist the mc_up / mc_ping_ms keys snapshot_bot_stats has been
-- writing into the in-memory history dict since the Minecraft monitor
-- landed — save_bot_stats_history never persisted them and the loader
-- never selected them, so the older-than-7-days tail of !graph minecraft
-- (which reads these keys once mc_ping_samples has pruned) was always
-- empty. NULL means "unknown": monitor disabled, no sample yet this
-- bucket, or a row written before this migration.
ALTER TABLE bot_stats_history ADD COLUMN IF NOT EXISTS mc_up TINYINT(1) NULL;
ALTER TABLE bot_stats_history ADD COLUMN IF NOT EXISTS mc_ping_ms DOUBLE NULL;
