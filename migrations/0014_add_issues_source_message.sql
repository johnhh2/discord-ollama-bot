-- 0014: persist the originating command message on each issue row.
--
-- channel_id/message_id refer to the embed posted in bug_report_channel
-- (admin-only). We also need to remember where the command that produced
-- the issue lives so the completion-DM jumplink lands the reporter in a
-- channel they can actually see (typically a public AI/game channel).
-- Pre-existing rows stay NULL and the DM falls back to a no-link body.

ALTER TABLE issues ADD COLUMN IF NOT EXISTS source_channel_id BIGINT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS source_message_id BIGINT;
