/* lakana
description: One row per customer order.
*/
SELECT * FROM (
  VALUES
    (1, 'open'),
    (2, 'shipped'),
    (3, 'cancelled')
) AS orders(order_id, status)
