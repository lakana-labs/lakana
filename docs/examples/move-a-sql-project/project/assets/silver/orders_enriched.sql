/* lakana
description: Every order with the name of its region. Each run adds the orders that are new.
write: append
watermark: order_id
columns:
  order_id: The order's number.
  region_name: The name of the region, empty when the region code is unknown.
  status: paid, refunded or pending.
  amount_cents: The order's amount in cents.
  order_date: The day of the order.
*/
SELECT
  orders.order_id,
  regions.region_name,
  orders.status,
  orders.amount_cents,
  orders.order_date
FROM silver.orders_clean AS orders
LEFT JOIN bronze.regions AS regions ON regions.region_code = orders.region_code
