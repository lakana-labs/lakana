/* lakana
description: Readings with the meter names written the same way.
kind: staging
*/
SELECT reading_id, upper(meter) AS meter, kwh
FROM bronze.readings
