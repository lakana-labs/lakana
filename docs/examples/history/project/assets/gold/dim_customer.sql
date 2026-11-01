/* lakana
description: Every version of every customer.
kind: dimension
grain: One row per customer and version of their details.
key: customer_id
write: history
valid_from: changed_at
close_missing: true
*/
SELECT customer_id, name, region, changed_at FROM silver.customers
