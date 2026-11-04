/* lakana
description: The stores.
kind: dimension
grain: One row per store.
key: shop_id
*/
SELECT store_id AS shop_id, name FROM bronze.stores
