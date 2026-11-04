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
    label: Net revenue
*/
SELECT date, store_id, SUM(gross_amount) AS gross_amount, SUM(net_amount) AS net_amount
FROM bronze.sales
GROUP BY date, store_id
