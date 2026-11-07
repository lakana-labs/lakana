/* lakana
description: The customers as they are now, the latest row of each, with its text trimmed.
kind: staging
key: customer_id
columns:
  customer_id: The customer's code.
  name: The customer's name.
  city: The city the customer lives in.
  changed_at: When the CRM recorded this row.
*/
SELECT customer_id, trim(name) AS name, trim(city) AS city, changed_at
FROM bronze.customers
QUALIFY row_number() OVER (PARTITION BY customer_id ORDER BY changed_at DESC) = 1
