/* lakana
description: One row per customer, kept up to date.
write: merge
key: [id]
*/
SELECT id, name, updated_at FROM bronze.customer_updates
