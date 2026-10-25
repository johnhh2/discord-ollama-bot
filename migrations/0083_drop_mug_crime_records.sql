-- 0083: !mug no longer competes for the shared `crime` record (the mugger
-- pays and pockets nothing, so its "payout" was just the amount spent), and
-- the rows mugs already hold would otherwise stand until something beat them.
-- JSON_VALUE, not JSON_EXTRACT: the latter keeps the member's quotes.
DELETE FROM records
 WHERE category = 'crime'
   AND JSON_VALUE(extra_json, '$.crime_type') = 'mug';
