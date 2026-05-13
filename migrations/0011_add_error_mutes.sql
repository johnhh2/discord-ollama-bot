-- 0011: error-mute table for auto-filed command-error bug reports.
--
-- When a command raises, `_log_command_error` files an entry in `issues`
-- the same way !bugreport does, and seeds a 🔇 reaction. A bot admin
-- adding 🔇 inserts a row here keyed by (command, exception type, exception
-- message). Removing 🔇 deletes the row. Before filing a new auto-report
-- the error path consults this table; matches are dropped on the floor.
--
-- The companion `mute_key` column on `issues` lets the reaction handler
-- (a) skip mute UI for non-error issues and (b) render the muted/unmuted
-- footer without rebuilding the key.

CREATE TABLE IF NOT EXISTS error_mutes (
    mute_key    VARCHAR(255) NOT NULL PRIMARY KEY,
    muted_by    BIGINT       NOT NULL,
    muted_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE issues ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'bug';
ALTER TABLE issues ADD COLUMN IF NOT EXISTS mute_key VARCHAR(255);
