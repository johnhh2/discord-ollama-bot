-- 0049: add 'global_admin' to the command_perms tier ENUM.
--
-- df5935a introduced the global_admin tier (VALID_TIERS + command_perms.json)
-- but never widened this ENUM, so the boot-time JSON->DB reconciliation in
-- init_db_state failed with (1265, "Data truncated for column 'tier'") and
-- the bot came up dropping every message. MODIFY is idempotent — re-running
-- it just re-applies the same definition.
ALTER TABLE command_perms
    MODIFY COLUMN tier ENUM('everyone','server_admin','bot_admin','global_admin')
    NOT NULL DEFAULT 'everyone';
