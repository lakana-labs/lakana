/* lakana
description: A sale sells at least one unit.
*/
SELECT sale_id, quantity FROM silver.sales WHERE quantity <= 0
