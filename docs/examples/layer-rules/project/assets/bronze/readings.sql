/* lakana
description: Meter readings as the meters sent them.
write: append
watermark: reading_id
*/
SELECT 1 AS reading_id, 'm-1' AS meter, 12.5 AS kwh
UNION ALL SELECT 2, 'm-2', 7.0
UNION ALL SELECT 3, 'm-1', 13.0
