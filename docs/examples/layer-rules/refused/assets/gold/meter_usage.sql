/* lakana
description: Energy used per meter.
kind: fact
grain: One row per meter.
key: [meter]
*/
SELECT upper(meter) AS meter, sum(kwh) AS kwh
FROM bronze.readings
GROUP BY upper(meter)
