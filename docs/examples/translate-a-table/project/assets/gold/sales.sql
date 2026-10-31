/* lakana
label: Sales per store
description: One row per store, with its net sales.
columns:
  store_id:
    label: Store
    description: The store that sold.
  net_amount: Sales after returns, in euros.
*/
SELECT store_id, SUM(net_amount) AS net_amount
FROM bronze.sales
GROUP BY store_id
