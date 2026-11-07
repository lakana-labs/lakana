/* lakana
label: Customers
description: Every version of every customer; a customer who moves gets a new row, so old sales keep the city they had.
kind: dimension
grain: One row per customer and version of their details.
key: customer_id
write: history
valid_from: changed_at
close_missing: true
columns:
  customer_id: The customer's code.
  name: The customer's name.
  city: The city the customer lived in during this version.
*/
SELECT customer_id, name, city, changed_at FROM silver.customers
