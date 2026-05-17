-- 0025: add `embed_msg_id` to chess_games so the bot can delete BOTH the
-- prior board image AND the prior text/embed message when bumping the
-- board to the bottom of the channel. board_msg_id tracks the image
-- attachment message; embed_msg_id tracks the embed message that holds
-- the title/last-move/captures text.

ALTER TABLE chess_games ADD COLUMN IF NOT EXISTS embed_msg_id BIGINT UNSIGNED NULL;
