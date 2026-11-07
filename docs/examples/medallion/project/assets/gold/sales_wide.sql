/* lakana
label: Sales, wide
description: One row per sale with its store, product and customer written out, for spreadsheets.
kind: wide
grain: One row per sale.
key: sale_id
columns:
  sale_id: The sale's number.
  sold_on: The day of the sale.
  store: The store's name.
  store_city: The city the store is in.
  product: The product's name.
  category: The product group.
  customer_id: The customer who bought.
  customer: The customer's name.
  customer_city: The city the customer lived in on the day of the sale.
  quantity: How many units were sold.
  gross_amount: Quantity times unit price in euros, before the discount.
  net_amount: The amount in euros after the discount.
*/
SELECT
  f.sale_id,
  f.sold_on,
  st.name AS store,
  st.city AS store_city,
  p.name AS product,
  p.category,
  f.customer_id,
  c.name AS customer,
  c.city AS customer_city,
  f.quantity,
  f.gross_amount,
  f.net_amount
FROM gold.fact_sales AS f
JOIN gold.dim_store AS st ON st.store_id = f.store_id
JOIN gold.dim_product AS p ON p.product_id = f.product_id
JOIN gold.dim_customer AS c
  ON c.customer_id = f.customer_id
  AND c.valid_from <= f.sold_on
  AND (c.valid_to IS NULL OR f.sold_on < c.valid_to)
