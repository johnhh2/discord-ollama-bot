-- 0086: insurance splits into three types. The existing 'insurance' rows are
-- personal insurance, which now only refunds crime losses; social insurance
-- takes over blocking the shop effects; property insurance covers repair
-- bills. Each type has its own policy and '_sub' rows. Nobody starts with
-- social cover: existing policyholders buy it like anyone else.
ALTER TABLE shop_effects
    MODIFY COLUMN effect_type ENUM('ragebait','mock','curse','tax','insurance','insurance_sub',
        'social_insurance','social_insurance_sub','property_insurance','property_insurance_sub') NOT NULL;
