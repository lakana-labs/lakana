/* lakana
description: The products as the shop system exports them; the price is still text.
write: merge
key: product_id
columns:
  product_id: The product's code.
  name: The product's name, as exported.
  category: The product group.
  unit_price: The price of one unit in euros, as text.
*/
SELECT * FROM (
  VALUES
    ('P-100', ' Coffee beans ', 'Food', '8.50'),
    ('P-200', 'Teapot', 'Home', '24.00'),
    ('P-300', 'Mug', 'Home', '6.00'),
    ('P-400', 'Notebook ', 'Office', '3.50')
) AS shop(product_id, name, category, unit_price)
