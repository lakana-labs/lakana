/* lakana
description: The stores, with their text trimmed.
kind: staging
key: store_id
columns:
  store_id: The store's number.
  name: The store's name.
  city: The city the store is in.
*/
SELECT CAST(store_id AS INTEGER) AS store_id, trim(name) AS name, trim(city) AS city
FROM bronze.stores
