-- Permanent per-user artifacts bought via !artifacts (shop). One row per
-- (user, artifact); quantity supports future stackable artifacts.
CREATE TABLE IF NOT EXISTS user_artifacts (
    user_id     BIGINT      NOT NULL,
    artifact_id VARCHAR(64) NOT NULL,
    quantity    INT         NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, artifact_id)
);
