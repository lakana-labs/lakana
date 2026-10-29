/* lakana
description: Three sales, written by hand.
*/
SELECT DATE '2026-10-01' AS date, 1 AS store_id, 120.0 AS amount
UNION ALL SELECT DATE '2026-10-01', 2, 80.0
UNION ALL SELECT DATE '2026-10-02', 1, 95.0
