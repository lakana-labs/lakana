/* lakana
description: A store's name has no stray spaces around it.
*/
SELECT store_id, name FROM silver.stores WHERE name <> trim(name)
