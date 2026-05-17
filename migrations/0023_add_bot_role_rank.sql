-- 0023: add per-guild rank to bot_roles so !shop roleup / !shop roledown can
-- swap adjacent ranks deterministically instead of comparing against Discord's
-- shared role.position (which includes non-bot roles and produced the
-- "#3 jumps to #1" bug when no other bot roles sat above the target).
--
-- New columns:
--   guild_id BIGINT UNSIGNED — which guild this role lives in. Role IDs are
--     globally unique, so we *could* keep PK as role_id and store guild_id as
--     a regular column, but ranking is inherently per-guild and the composite
--     PK matches that mental model.
--   rank_pos INT — 1 = highest in the bot-role ranking. Lower number = higher
--     rank. Gaps are allowed (the bot doesn't compact on delete). Named
--     rank_pos rather than `rank` to avoid the MariaDB 10.2+ reserved word.
--
-- Both default to 0 for existing rows; production data will be backfilled
-- manually by the operator. New rows written by !shop createrole get the
-- correct values from app code.

ALTER TABLE bot_roles ADD COLUMN IF NOT EXISTS guild_id BIGINT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE bot_roles ADD COLUMN IF NOT EXISTS rank_pos INT NOT NULL DEFAULT 0;
ALTER TABLE bot_roles DROP PRIMARY KEY;
ALTER TABLE bot_roles ADD PRIMARY KEY (guild_id, role_id);
