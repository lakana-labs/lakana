/* lakana
description: The customers as the CRM shows them today, with when each was last changed.
*/
SELECT * FROM (
  VALUES
    ('C-001', 'Ada', 'north', TIMESTAMPTZ '2026-01-01 12:00:00+00'),
    ('C-002', 'Grace', 'north', TIMESTAMPTZ '2026-01-01 12:00:00+00')
) AS crm(customer_id, name, region, changed_at)
