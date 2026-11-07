/* lakana
description: The stores as the shop system exports them, with the stray spaces it leaves in text.
write: merge
key: store_id
columns:
  store_id: The store's number.
  name: The store's name, as exported.
  city: The city the store is in, as exported.
*/
SELECT * FROM (
  VALUES (1, ' Harbour ', 'Hamburg'), (2, 'Station', 'Berlin '), (3, 'Airport', 'Munich')
) AS shop(store_id, name, city)
