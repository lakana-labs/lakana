/* lakana
description: Sales per store, product and day.
kind: fact
grain: One row per store, product and calendar day.
key: [date, store_id, sku]
references:
  store_id: gold.dim_store
  sku: gold.dim_product
checks:
  references: warn
*/
SELECT date, store_id, sku, SUM(amount) AS amount
FROM bronze.sales
GROUP BY date, store_id, sku
