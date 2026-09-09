-- 0066: feature_request_watchers — users who reacted 👀 on a !featurerequest
-- embed to follow it.
--
-- The requester is always notified when their request is completed or
-- rejected; a watcher row extends the same DMs to anyone else who wants
-- them. Keyed by the request embed's message_id (feature_requests.message_id
-- is UNIQUE) so the raw reaction handlers can add/remove rows straight from
-- the payload. Removing the 👀 reaction deletes the row.

CREATE TABLE IF NOT EXISTS feature_request_watchers (
    message_id  BIGINT    NOT NULL,
    user_id     BIGINT    NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
