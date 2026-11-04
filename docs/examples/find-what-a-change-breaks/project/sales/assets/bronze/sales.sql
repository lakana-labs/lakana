/* lakana
description: Three sales, written by hand.
*/
SELECT DATE '2026-10-01' AS date, 1 AS store_id, 100.0 AS net_amount
UNION ALL SELECT DATE '2026-10-01', 2, 70.0
UNION ALL SELECT DATE '2026-10-02', 1, 90.0
