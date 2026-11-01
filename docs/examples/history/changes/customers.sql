/* lakana
description: The customers as the CRM shows them today, with when each was last changed.
*/
SELECT * FROM (
  VALUES
    ('C-002', 'Grace', 'south', TIMESTAMPTZ '2026-02-01 12:00:00+00'),
    ('C-003', 'Edsger', 'west', TIMESTAMPTZ '2026-02-01 12:00:00+00')
) AS crm(customer_id, name, region, changed_at)
