-- 0013: feature_requests — user-submitted ideas reviewed in a per-guild
-- channel before being promoted into a feature issue.
--
-- Lifecycle:
--   • !featurerequest posts an embed into `feature_request_channel` and
--     inserts a row here with status='open'.
--   • A bot admin reacting ✅ flips status='accepted', spawns a kind='feature'
--     row in `issues`, and writes its id back into feature_issue_id.
--   • Reacting 🛑 flips status='rejected' (no feature issue is spawned).
--   • While a feature issue is linked, status-change reactions on the issue
--     re-render the request embed so the reporter can track progress.

CREATE TABLE IF NOT EXISTS feature_requests (
    id                BIGINT      NOT NULL AUTO_INCREMENT PRIMARY KEY,
    guild_id          BIGINT      NOT NULL,
    channel_id        BIGINT      NOT NULL,
    message_id        BIGINT      NOT NULL UNIQUE,
    reporter_id       BIGINT      NOT NULL,
    description       TEXT        NOT NULL,
    status            VARCHAR(16) NOT NULL DEFAULT 'open',
    feature_issue_id  BIGINT,
    created_at        TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_by       BIGINT,
    resolved_at       TIMESTAMP   NULL DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
