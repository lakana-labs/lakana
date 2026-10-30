/* lakana
description: Three products, written by hand; one is listed twice.
*/
SELECT 'tea' AS sku, 'Green tea' AS name
UNION ALL SELECT 'cake', 'Carrot cake'
UNION ALL SELECT 'soup', 'Tomato soup'
UNION ALL SELECT 'soup', 'Soup of the day'
