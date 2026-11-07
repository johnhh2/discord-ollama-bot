-- 0090: once-a-day limit on shop effects that make the bot post on the
-- target's messages (ragebait, mock, curse, tax). JSON {item: CT day} of the
-- buyer's last purchase of each.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS targeted_buy_days TEXT NULL;
