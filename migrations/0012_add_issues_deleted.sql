-- 0012: soft-delete flag for issues hidden via `!issue delete <N>`.
--
-- Rather than DELETE the row (and lose the message_id binding that the
-- reaction listener depends on), soft-deletes set `deleted=1` and the
-- !issues listing filters them out. The embed itself is left untouched
-- in Discord and reactions on it become no-ops because get_issue_by_message
-- still resolves to a row that callers now ignore (or simply skip via
-- the deleted flag).

ALTER TABLE issues ADD COLUMN IF NOT EXISTS deleted TINYINT NOT NULL DEFAULT 0;
