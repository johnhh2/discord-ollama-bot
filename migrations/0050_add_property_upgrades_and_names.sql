-- 0050: property upgrades + custom business names (!assets upgrade / rename).
--
-- `upgraded` marks that the owner bought the property's one predefined
-- upgrade (name/cost/boost are hardcoded per property in src/properties.py);
-- the upgrade's cost folds into the property's value and its boost into the
-- property's daily revenue. `custom_name` is an owner-chosen display name
-- for the business (NULL = catalog name); it travels with the deed on a
-- marketplace sale and is cleared when the bank buys the property back.
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS upgraded    BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE property_owners ADD COLUMN IF NOT EXISTS custom_name VARCHAR(64) NULL;
