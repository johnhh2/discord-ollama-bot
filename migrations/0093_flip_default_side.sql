-- !flip default heads|tails: the side a flip calls when none is typed,
-- including the dailies 🪙 stake. NULL means heads.
ALTER TABLE economy_users ADD COLUMN IF NOT EXISTS flip_side VARCHAR(5) NULL;
