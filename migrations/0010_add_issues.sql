-- 0010: persistent !bug / !issue reports.
--
-- Stores the bug-report embed reference so the on_raw_reaction_add handler
-- can find the row when a bot admin reacts with ✅, 🚧, or ❌ to mark the
-- issue completed / work-in-progress / rejected. status is persisted so a
-- reboot doesn't lose admin triage — the embed is re-rendered in the
-- matching color on the next status transition.

CREATE TABLE IF NOT EXISTS issues (
    id           BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    guild_id     BIGINT,
    channel_id   BIGINT      NOT NULL,
    message_id   BIGINT      NOT NULL UNIQUE,
    reporter_id  BIGINT      NOT NULL,
    report       TEXT        NOT NULL,
    status       VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at   TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_by  BIGINT,
    resolved_at  TIMESTAMP   NULL DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
