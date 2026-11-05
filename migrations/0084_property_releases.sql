-- 0084: the property release pool (src/property_supply.py).
--
-- One row per pool deed the 5am release pass has admitted to the market.
-- Shipped deeds have no row: they were on the market before the pool
-- existed and are always released. Rows are never deleted — a release is
-- permanent even if the economy shrinks. The pass's day marker and the
-- per-player cap high-water mark live in economy_meta
-- (last_property_release, property_cap), which needs no schema change.
CREATE TABLE IF NOT EXISTS property_releases (
    property_id VARCHAR(64) NOT NULL PRIMARY KEY,
    released_at DOUBLE      NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
