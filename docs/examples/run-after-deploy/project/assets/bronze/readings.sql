/* lakana
description: Six temperature readings from three weather stations, written by hand.
columns:
  station_id: The station that took the reading.
  temp_c: The temperature in degrees Celsius.
*/
SELECT 'A1' AS station_id, 4.0 AS temp_c
UNION ALL SELECT 'A1', 6.0
UNION ALL SELECT 'B2', 1.0
UNION ALL SELECT 'B2', 3.0
UNION ALL SELECT 'C3', 2.0
UNION ALL SELECT 'C3', 999.0
