/* lakana
description: Sales straight from bronze.
kind: fact
grain: One row per sale.
key: sale_id
*/
SELECT sale_id, store_id FROM bronze.sales
