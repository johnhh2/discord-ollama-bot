-- 0048: real-estate property system (!assets).
--
-- property_owners holds one row per OWNED property — the catalog itself lives
-- in code (src/properties.py), keyed by property_id. The PK on property_id is
-- the bot-wide uniqueness guarantee: every property has at most one owner
-- across all servers. list_price NULL means "not for sale"; a non-NULL value
-- is a live cross-server marketplace listing.
CREATE TABLE IF NOT EXISTS property_owners (
    property_id VARCHAR(64)     NOT NULL PRIMARY KEY,
    owner_id    BIGINT UNSIGNED NOT NULL,
    acquired_at DOUBLE          NOT NULL,
    list_price  BIGINT          NULL,
    listed_at   DOUBLE          NULL,
    INDEX idx_property_owner (owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Per-user revenue bookkeeping: property_paid_at is the timestamp revenue
-- last accrued FROM (stamped on every !daily payout); property_revenue_total
-- is the lifetime sum of property revenue banked (feeds !graph assets).
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS property_paid_at       DOUBLE NOT NULL DEFAULT 0;
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS property_revenue_total BIGINT NOT NULL DEFAULT 0;

-- Graph history: per-(date, bucket, user) property book value and lifetime
-- revenue snapshots, alongside the existing wallet/savings columns.
ALTER TABLE balance_history ADD COLUMN IF NOT EXISTS assets        BIGINT NOT NULL DEFAULT 0;
ALTER TABLE balance_history ADD COLUMN IF NOT EXISTS asset_revenue BIGINT NOT NULL DEFAULT 0;
