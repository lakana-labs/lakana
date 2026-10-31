/* lakana
description: Energy used per meter.
kind: fact
grain: One row per meter.
key: [meter]
*/
SELECT meter, sum(kwh) AS kwh
FROM silver.readings
GROUP BY meter
