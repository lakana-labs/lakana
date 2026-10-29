/* lakana
description: Sales per store and day.
kind: fact
grain: One row per store and day.
key: [date, store_id]
references:
  store_id: gold.dim_store
*/
SELECT date, store_id, SUM(amount) AS amount FROM bronze.sales GROUP BY date, store_id
