/* lakana
description: The customers as the source system shows them today.
*/
SELECT * FROM (
  VALUES
    (1, 'Ada', TIMESTAMP '2026-01-01 09:00:00'),
    (2, 'Grace Hopper', TIMESTAMP '2026-01-02 10:00:00'),
    (3, 'Edsger', TIMESTAMP '2026-01-02 11:00:00')
) AS source(id, name, updated_at)
