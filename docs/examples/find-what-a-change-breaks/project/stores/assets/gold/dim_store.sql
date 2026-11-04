/* lakana
description: The stores.
kind: dimension
grain: One row per store.
key: store_id
*/
SELECT store_id, name FROM bronze.stores
