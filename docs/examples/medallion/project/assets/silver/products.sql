/* lakana
description: The products, with their text trimmed and the price as a number.
kind: staging
key: product_id
columns:
  product_id: The product's code.
  name: The product's name.
  category: The product group.
  unit_price: The price of one unit in euros.
*/
SELECT product_id, trim(name) AS name, category, CAST(unit_price AS DECIMAL(10, 2)) AS unit_price
FROM bronze.products
