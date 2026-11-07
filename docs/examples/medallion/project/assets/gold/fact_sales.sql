/* lakana
label: Sales
description: One row per sale, pointing at the store, the product and the customer as they were on the day.
kind: fact
grain: One row per sale.
key: sale_id
references:
  store_id: gold.dim_store
  product_id: gold.dim_product
  customer_id: {to: gold.dim_customer, at: sold_on}
measures:
  net_amount:
    aggregation: sum
    currency: EUR
    label: Net revenue
columns:
  sale_id: The sale's number.
  store_id: The store that sold.
  customer_id: The customer who bought.
  product_id: The product sold.
  sold_on: The day of the sale.
  quantity: How many units were sold.
  gross_amount: Quantity times unit price in euros, before the discount.
  net_amount: The amount in euros after the discount.
# health: {volume: block}   # uncomment to stop a run whose row count is far outside its usual band
*/
SELECT
  s.sale_id,
  s.store_id,
  s.customer_id,
  s.product_id,
  s.sold_on,
  s.quantity,
  CAST(s.quantity * p.unit_price AS DECIMAL(12, 2)) AS gross_amount,
  CAST(s.quantity * p.unit_price - s.discount AS DECIMAL(12, 2)) AS net_amount
FROM silver.sales AS s
LEFT JOIN silver.products AS p ON p.product_id = s.product_id
