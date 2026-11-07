-- 0092: Minecraft, V Rising and Vintage Story notices share one channel
-- (`!settings channel game-servers`); each guild keeps the channel it had set
-- for Minecraft. CAST keeps the id a JSON number, as the bot writes it; the
-- JSON_TYPE guard skips a cleared (null) channel, which CAST would reject.
UPDATE guild_settings
   SET settings_json = JSON_REMOVE(
         JSON_SET(settings_json, '$.game_servers_channel',
                  CAST(JSON_VALUE(settings_json, '$.minecraft_channel') AS UNSIGNED)),
         '$.minecraft_channel')
 WHERE UPPER(JSON_TYPE(JSON_EXTRACT(settings_json, '$.minecraft_channel'))) = 'INTEGER'
   AND JSON_EXTRACT(settings_json, '$.game_servers_channel') IS NULL;
