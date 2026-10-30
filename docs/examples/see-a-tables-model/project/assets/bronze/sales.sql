/* lakana
description: Four sales, written by hand. The last one is of a product the shop does not list.
*/
SELECT DATE '2026-10-01' AS date, 1 AS store_id, 'tea' AS sku, 120.0 AS amount
UNION ALL SELECT DATE '2026-10-01', 2, 'tea', 80.0
UNION ALL SELECT DATE '2026-10-02', 1, 'cake', 95.0
UNION ALL SELECT DATE '2026-10-02', 2, 'soup', 40.0
