-- The gameplay day (CT, 5am rollover) of the player's last successful bank
-- heist: one successful heist per player per day.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS heist_success_day VARCHAR(10) NULL;
