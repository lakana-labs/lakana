/* lakana
description: The sales with the right types, and each sale once (the latest export of it).
kind: staging
key: sale_id
columns:
  sale_id: The sale's number.
  store_id: The store that sold.
  customer_id: The customer who bought.
  product_id: The product sold.
  quantity: How many units were sold.
  sold_on: The day of the sale.
  discount: The discount in euros.
*/
SELECT
  CAST(sale_id AS INTEGER) AS sale_id,
  CAST(store_id AS INTEGER) AS store_id,
  trim(customer_id) AS customer_id,
  trim(product_id) AS product_id,
  CAST(quantity AS INTEGER) AS quantity,
  CAST(sold_on AS DATE) AS sold_on,
  CAST(discount AS DECIMAL(10, 2)) AS discount
FROM bronze.sales
QUALIFY row_number() OVER (PARTITION BY sale_id ORDER BY exported_at DESC) = 1
