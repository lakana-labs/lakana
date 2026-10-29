/* lakana
description: The products.
kind: dimension
grain: One row per product.
key: none
*/
SELECT sku, name FROM bronze.products
