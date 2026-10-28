/* lakana
description: The average temperature of each station.
columns:
  station_id: The station that took the readings.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT station_id, AVG(temp_c) AS avg_temp_c
FROM bronze.readings
GROUP BY station_id
