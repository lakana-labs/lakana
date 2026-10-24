/* lakana
description: Every change of a customer, oldest first; each run adds the new ones.
write: append
watermark: updated_at
*/
SELECT id, name, updated_at FROM bronze.customer_updates
