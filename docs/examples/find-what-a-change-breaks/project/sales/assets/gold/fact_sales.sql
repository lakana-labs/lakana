/* lakana
description: Sales per store and day.
kind: fact
grain: One row per store and calendar day.
key: [date, store_id]
references:
  store_id: gold.dim_store
measures:
  net_amount:
    aggregation: sum
    currency: EUR
*/
SELECT s.date, s.store_id, SUM(s.net_amount) AS net_amount
FROM bronze.sales AS s
JOIN gold.dim_store AS d ON d.store_id = s.store_id
GROUP BY s.date, s.store_id
