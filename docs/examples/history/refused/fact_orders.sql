/* lakana
description: One row per order, pointing at the customer as they were when they ordered.
kind: fact
grain: One row per order.
key: order_id
references:
  customer_id: gold.dim_customer
*/
SELECT order_id, customer_id, ordered_at FROM silver.orders
