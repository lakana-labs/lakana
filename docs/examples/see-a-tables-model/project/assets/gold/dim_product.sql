/* lakana
description: The products.
kind: dimension
grain: One row per product.
key: sku
*/
SELECT sku, name FROM bronze.products
