-- 0087: the second property upgrade type. `secured` marks that the owner
-- bought the deed's security upgrade (src/properties.py
-- PROPERTY_SECURITY_UPGRADES); it travels with the deed like `upgraded`.
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS secured BOOLEAN NOT NULL DEFAULT FALSE;
