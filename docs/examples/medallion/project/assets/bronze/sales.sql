/* lakana
description: Twelve generated sales as the till exports them (everything is text), and sale 12 exported twice.
write: merge
key: [sale_id, exported_at]
columns:
  sale_id: The sale's number.
  store_id: The store that sold, as text.
  customer_id: The customer who bought.
  product_id: The product sold.
  quantity: How many units, as text.
  sold_on: The day of the sale, as text.
  discount: The discount in euros, as text.
  exported_at: When the till exported this row, as text.
*/
-- To load a file instead, replace this file with a Python asset that reads it with
-- `lakana.ingest.files.read`: https://lakana-labs.github.io/lakana/how-to/load-data-from-a-source/
WITH generated AS (
  SELECT
    CAST(n AS VARCHAR) AS sale_id,
    CAST(n % 3 + 1 AS VARCHAR) AS store_id,
    'C-00' || CAST((n * 2) % 3 + 1 AS VARCHAR) AS customer_id,
    'P-' || CAST(n % 4 + 1 AS VARCHAR) || '00' AS product_id,
    CAST(n % 3 + 1 AS VARCHAR) AS quantity,
    CAST(DATE '2026-10-01' + CAST(n AS INTEGER) AS VARCHAR) AS sold_on,
    CAST(n % 3 AS VARCHAR) || '.00' AS discount,
    '2026-10-20 08:00:00' AS exported_at
  FROM range(1, 13) AS days(n)
)
SELECT * FROM generated
UNION ALL
SELECT sale_id, store_id, customer_id, product_id, quantity, sold_on, discount,
  '2026-10-21 08:00:00' AS exported_at
FROM generated
WHERE sale_id = '12'
