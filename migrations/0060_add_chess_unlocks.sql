-- Chess shop unlocks: per-user and global (no guild dimension, like the
-- economy). item_id is 'pieces:<key>' or 'board:<key>' from the catalog in
-- src/chess_shop.py. Cost-0 defaults (cburnett pieces, default board) are
-- owned by everyone and never written here.
CREATE TABLE IF NOT EXISTS chess_unlocks (
    user_id BIGINT NOT NULL,
    item_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, item_id)
);
