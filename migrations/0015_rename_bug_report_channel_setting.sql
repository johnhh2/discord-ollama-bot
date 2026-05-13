-- 0015: rename the global bot setting key from `bug_report_channel` to
-- `internal_issue_channel`. The channel's purpose has broadened beyond bug
-- reports (it now also hosts !issue feature/task/improvement embeds and
-- spawned feature issues from feature requests), so the user-facing setting
-- was renamed to match.
--
-- The migration runner records a checksum and only executes this file once,
-- so a plain UPDATE is safe — on second boot the schema_migrations row
-- prevents re-execution.

UPDATE bot_settings
   SET key_name = 'internal_issue_channel'
 WHERE key_name = 'bug_report_channel';
