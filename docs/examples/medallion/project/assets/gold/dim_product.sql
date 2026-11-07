/* lakana
label: Products
description: The products we sell, with their current price.
kind: dimension
grain: One row per product.
key: product_id
columns:
  product_id: The product's code, such as P-100.
  name: The product's name.
  category: The product group.
  unit_price: The price of one unit in euros.
*/
SELECT product_id, name, category, unit_price FROM silver.products
