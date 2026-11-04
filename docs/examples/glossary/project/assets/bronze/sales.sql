/* lakana
description: Four sales, written by hand.
*/
SELECT DATE '2026-10-01' AS date, 1 AS store_id, 120.0 AS gross_amount, 100.0 AS net_amount
UNION ALL SELECT DATE '2026-10-01', 2, 80.0, 70.0
UNION ALL SELECT DATE '2026-10-02', 1, 95.0, 90.0
UNION ALL SELECT DATE '2026-10-02', 2, 40.0, 40.0
