/* lakana
description: A customer's name has no stray spaces around it.
*/
SELECT customer_id, name FROM silver.customers WHERE name <> trim(name)
