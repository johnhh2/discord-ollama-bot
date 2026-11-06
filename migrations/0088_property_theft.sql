-- 0088: property theft (src/property_theft.py). A successful theft damages
-- the deed until it is repaired: damaged_at is when, repair_cost the bill,
-- and repair_cover_pct / repair_cover_cap the owner's property insurance at
-- that moment, so a policy bought afterwards pays nothing toward it.
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS damaged_at DOUBLE NULL;
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS repair_cost BIGINT NOT NULL DEFAULT 0;
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS repair_cover_pct INT NOT NULL DEFAULT 0;
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS repair_cover_cap BIGINT NOT NULL DEFAULT 0;
-- One successful property theft per thief per gameplay day (CT, 5am).
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS property_theft_day VARCHAR(10) NULL;
-- A fixed bail price for the current jail; NULL prices it by the usual
-- formula from bail_amount. Property theft sets it (twice the take).
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS bail_price BIGINT NULL;
