/* lakana
description: The customers as the CRM exports them, with a row each time a customer changed.
write: merge
key: [customer_id, changed_at]
columns:
  customer_id: The customer's code.
  name: The customer's name, as exported.
  city: The city the customer lived in at that change.
  changed_at: When the CRM recorded this row.
*/
SELECT * FROM (
  VALUES
    ('C-001', ' Ada ', 'Hamburg', TIMESTAMPTZ '2026-09-01 12:00:00+00'),
    ('C-002', 'Grace', 'Berlin', TIMESTAMPTZ '2026-09-01 12:00:00+00'),
    ('C-003', 'Edsger', 'Dresden', TIMESTAMPTZ '2026-08-01 12:00:00+00'),
    ('C-003', 'Edsger', 'Munich', TIMESTAMPTZ '2026-09-01 12:00:00+00')
) AS crm(customer_id, name, city, changed_at)
