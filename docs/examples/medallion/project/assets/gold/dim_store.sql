/* lakana
label: Stores
description: The stores, one row each.
kind: dimension
grain: One row per store.
key: store_id
columns:
  store_id: The store's number.
  name: The store's name.
  city: The city the store is in.
*/
SELECT store_id, name, city FROM silver.stores
