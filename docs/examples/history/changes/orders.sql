/* lakana
description: The orders as the shop shows them.
*/
SELECT * FROM (
  VALUES
    (1, 'C-001', DATE '2026-01-10'),
    (2, 'C-002', DATE '2026-01-15'),
    (3, 'C-002', DATE '2025-12-01')
) AS shop(order_id, customer_id, ordered_at)
