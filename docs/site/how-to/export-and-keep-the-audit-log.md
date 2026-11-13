# Export and keep the audit log

After this guide you know what Lakana's audit log records and what it does not, you can copy the
log out of the instance into files on a schedule, you have a person or service account that may
read it and nobody else does, and you know what the log does not protect against. You need an
administrator's profile (the `ADMIN` role, which holds the right to read the log), the `lakana`
CLI and, for the schedule, a machine with `cron` or another scheduler.

Lakana describes what it records here. It is a record kept by the instance, not an
audit certificate: whether it meets a rule you must follow is for you and your auditor to judge.

## What the log records

Each event has a time, the principal it is attributed to, a type, the object it concerns
(environment, namespace, table) and a `detail` that depends on the type. The types are listed
in the `AuditEventType` of `lakana_core.models`; in groups:

| Group | Events |
|---|---|
| Sign-in | `AUTHENTICATION_SUCCEEDED`, `AUTHENTICATION_FAILED`, `OIDC_TOKEN_EXCHANGED`, `OIDC_EXCHANGE_REFUSED`, `SESSION_STARTED`, `SESSION_ENDED` (`detail.reason` says why), `SIGN_IN_LINK_*`, `PASSKEY_*`, `SIGN_IN_SETTINGS_CHANGED` |
| Refusals | `AUTHORIZATION_DENIED`, `PROTECTED_WRITE_DENIED`, `CODE_INTEGRITY_FAILED`, `CONNECTION_CREDENTIAL_REFUSED` |
| Access | `GRANT_CREATED`, `GRANT_REVOKED`, `GROUP_*`, `PRINCIPAL_*`, `PAT_*`, `TASK_TOKEN_*`, `ACCESS_APPLIED`, `MANAGED_SCOPE_OVERRIDDEN`, `BOOTSTRAP_ADMIN_RESTORED`, `CI_TRUST_RULE_*` |
| Policies | `ROW_FILTER_*`, `COLUMN_MASK_*` |
| Secrets and connections | `SECRET_*`, `CONNECTION_*`, `PACKAGE_INDEX_*`, `REPOSITORY_*` |
| Environments and tables | `ENVIRONMENT_*`, `TABLE_WRITE`, `TABLE_APPENDED`, `TABLE_SCHEMA_CHANGED`, `TABLE_MAINTAINED`, `TABLE_DROPPED`, `TABLE_UNDROPPED`, `TABLE_PURGED`, `TABLE_ROLLED_BACK`, `TABLE_RETENTION_*`, `TABLE_KEPT*`, `TABLE_DESCRIBED`, `COLUMN_DESCRIBED`, `TABLE_LOCATION_ABANDONED`, `ORPHANED_LOCATIONS_DELETED` |
| Code and runs | `DEPLOYED`, `DEPLOY_DRY_RUN`, `ROLLED_BACK`, `UNDEPLOYED`, `RUN_*`, `RUN_AS_*`, `BREAK_GLASS_WRITE`, `CHECK_FAILED`, `JOB_*`, `NOTIFICATION_*` |
| Queries | `QUERY_COMPLETED` (also for a query that failed: `detail.status` says), `QUERY_CANCELLED` |
| Artifacts | `ARTIFACT_*` |
| Reading the log and other people's activity | `ACTIVITY_VIEWED` |
| Reading the file that defines a table | `SOURCE_VIEWED` (repository, path and commit; once per person, file and commit per hour) |

What the log does **not** record:

- **A table's rows read, by whom, row by row.** A query is one `QUERY_COMPLETED` event with the
  tables it touched and the number of rows it returned. Loading a table through the Iceberg REST
  catalog is no event, and neither is listing tables, namespaces or environments. A task's write
  whose worker died without evidence of a commit ends failed and has no `QUERY_COMPLETED` event.
- **Creating or renaming a table, creating, changing or dropping a namespace.** The code of these
  operations writes no event (the other changes in the table above do).
- **A data commit by an Iceberg client** that Lakana did not make as a write of its own: there is
  no event for the commit. A commit that removes or replaces a column is recorded as
  `TABLE_SCHEMA_CHANGED`.
- **All of a burst of refusals.** To keep the log bounded, an `AUTHORIZATION_DENIED` past
  `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` (30 by default) per principal per minute is
  refused but not written to the table; it goes to the process log, and the next event says how
  many were left out. The first refusal of each action is always written.
- **Reading other people's events, when switched off.** `LAKANA_VIEW_AUDIT__ENABLED=false` stops
  the `ACTIVITY_VIEWED` events (`true` by default); the window is `LAKANA_VIEW_AUDIT__WINDOW_MINUTES`.
  It never stops `SOURCE_VIEWED`: a read of a deployed file is always recorded, with the same window.
- **Each successful sign-in.** One per token every 15 minutes (`LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES`;
  `0` records every request), with the number of those left out in `detail.suppressed`.
- **Query text, unless you keep it.** `LAKANA_AUDIT__QUERY_TEXT` is `full` by default; `hash` stores
  the SHA-256 and the length, `off` nothing. A change applies to new events only.
- **What happens outside Lakana:** a person with the database password, or the storage keys, or
  `docker` on the machine, acts without Lakana's API and so without an event. See "What the log
  does not protect against".

The details of the settings are in [Settings](../reference/settings.md) and
[Lists and audit](../reference/lists.md#audit); the reasons for each rule are in
[Security](../explanation/security.md#what-the-audit-log-records).

## Who may read the log

Reading needs the right `READ_AUDIT_LOG` on the instance, which the `ADMIN` role holds. Seeing the
SQL text of **other people's** queries and the watermarks of their writes needs one more right,
`READ_QUERY_TEXT`, which no role holds: without it an export shows each query as its SHA-256 and
length. Decide whether your export needs the text; if it does, grant `READ_QUERY_TEXT` to the one
account that exports, and keep the files as protected as the tables they were queried from.

Give the export its own service account, with only the right to read the log:

<!-- source: docs/examples/audit-export/run.sh#auditor -->
```bash
lakana principal create auditor --type service-account
lakana grant create --kind INSTANCE --principal auditor --action READ_AUDIT_LOG
lakana token issue auditor audit-export
```

The last command prints the token once. Keep it where only the scheduled job can read it.

Reading other people's events is itself recorded, unless `LAKANA_VIEW_AUDIT__ENABLED=false` (`ACTIVITY_VIEWED`, once per person and view per
hour), and if that event cannot be written the read is refused. Take the right away from a person
with `lakana grant revoke`. `lakana access explain NAME` shows what a principal may do.

## Export the log

`lakana audit list` pages through the events, newest first. With `--output json` each event
carries its `detail`; the default table drops it. `--all` follows every page:

<!-- source: docs/examples/audit-export/run.sh#export-all -->
```bash
lakana --output json audit list --all > audit-all.json
```

The file is a JSON list of events. These are the two events of one type in a new instance:

<!-- output: audit-export#one-event -->
```text
[
  {
    "id": "<uuid>",
    "occurred_at": "<timestamp>",
    "principal_id": "<uuid>",
    "event_type": "PRINCIPAL_CREATED",
    "securable_kind": null,
    "securable_environment": null,
    "securable_namespace": null,
    "securable_table": null,
    "detail": {
      "principal": "auditor",
      "type": "SERVICE_ACCOUNT"
    }
  },
  {
    "id": "<uuid>",
    "occurred_at": "<timestamp>",
    "principal_id": "<uuid>",
    "event_type": "PRINCIPAL_CREATED",
    "securable_kind": null,
    "securable_environment": null,
    "securable_namespace": null,
    "securable_table": null,
    "detail": {
      "principal": "admin",
      "type": "USER"
    }
  }
]
```

Narrow it with `--since` and `--until` (ISO 8601, UTC when there is no offset; both ends
are included), `--event-type`, `--kind connection`, `--env` and `--principal-id`
([the command](../reference/cli.md#lakana-audit-list)). A period with no events gives `[]`:

<!-- source: docs/examples/audit-export/run.sh#export-period -->
```bash
lakana --output json audit list --since 2020-01-01 --until 2020-02-01 --all > audit-2020-01.json
```

Count what you got:

<!-- source: docs/examples/audit-export/run.sh#check-count -->
```bash
python3 -c 'import json; print(len(json.load(open("audit-all.json"))), "events")'
```

Without the CLI, the route is `GET /api/v1/audit-events` (the
[REST API reference](../reference/rest-api.md)). It takes `event_type`, `kind`, `environment`,
`principal_id`, `since`, `until`, `limit` (1 to 500, 100 by default) and `cursor`. The body is
`{"items": [...], "next_cursor": ...}`; send the same filters with the `next_cursor` of the last
page until it is `null`. It answers `403` without `READ_AUDIT_LOG` and `503` when a read that must
be recorded could not be.

## Export every month, and keep the files

The script below writes one file per calendar month (UTC) and the SHA-256 of that file.
Run it after the month has ended, so the file is complete:

<!-- source: docs/examples/audit-export/export-month.sh -->
```bash
#!/usr/bin/env bash
# Usage: export-month.sh YYYY-MM
# Writes audit-YYYY-MM.json (every audit event of that month, UTC, as JSON with its detail)
# and its SHA-256 in audit-YYYY-MM.json.sha256. Needs the `lakana` CLI signed in as someone
# who holds READ_AUDIT_LOG, and python3.
set -euo pipefail
month=${1:?usage: export-month.sh YYYY-MM}
since="$month-01T00:00:00+00:00"
until=$(python3 - "$month" <<'PY'
import sys
from datetime import datetime, timedelta, timezone
year, month = map(int, sys.argv[1].split("-"))
first = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
print((first - timedelta(microseconds=1)).isoformat())
PY
)
lakana --output json audit list --since "$since" --until "$until" --all > "audit-$month.json"
sha256sum "audit-$month.json" > "audit-$month.json.sha256"
```

<!-- source: docs/examples/audit-export/run.sh#month -->
```bash
./export-month.sh 2026-10
```

To run it on the second day of each month at 03:00 for the month before, with the
service account's token (here `date` is GNU `date`; on macOS use another way to name last month):

<!-- not tested: a crontab line of the operator's machine; the script it runs is tested in the example audit-export -->
```bash
0 3 2 * * cd /srv/lakana-audit && LAKANA_URL=https://lakana.example.org LAKANA_TOKEN="$(cat /etc/lakana-audit.token)" ./export-month.sh "$(date -u -d 'last month' +\%Y-\%m)"
```

(In a crontab `%` ends the command, so it is written `\%`.) `LAKANA_URL` and `LAKANA_TOKEN`
together stand in for a profile; the CLI refuses to start if only one is set.

Then keep the files for as long as you must keep them. Lakana does not delete audit events: the
code has no way to remove or change one, and a table of events only grows. So the retention
period is a rule for your copies. Some practical points:

- **Copy the files off the machine,** to storage that is not the instance's own and that the
  people who run the instance cannot change. Lakana has no such storage.
- **Keep the checksum files** apart from the exports. A later match shows the file was not changed
  by accident or since; it is not protection against someone who can change both.
- **Back up the catalog** as well (the database holds the log): [Backups](deploy-with-compose.md#backups).
  Restoring an old backup brings back an old log, and the events after that backup are lost with the
  lost state. Exports of the months since the backup are then the only copy.
- **Plan for the size.** A busy instance writes events for each query and each run, and the table is
  never pruned. Lower what it writes with the settings above if the volume is a problem, knowing
  that those events are then not recorded.

## What the log does not protect against

**The audit log has no tamper-evidence yet.** Events are append-only in the sense that Lakana's
code offers no way to change or delete one (`AuditService` has no update or delete method), and
the API has no route for it. But the log is rows in a Postgres table: nothing in the database
stops a person with the database password from changing or deleting rows, no event would record
it, and nothing in the exported files would show it afterwards. Integrity protection, which would
make a change detectable, is not part of v1.0.0; it is planned for after it ([#495](https://github.com/lakana-labs/lakana/issues/495)). Until then:

- Restrict who holds the database password (`POSTGRES_PASSWORD` in the bundle's `.env`) and the
  machine's `docker` access, as you restrict the right to read the log.
- Export on a schedule to storage you trust more than the instance, so a change in the instance
  can be found by comparing the exports with the log.
- Do not describe the log to others as tamper-proof. It is not.

Next: [What leaves the machine](../explanation/what-leaves-the-machine.md) says where the log and
the other state are stored; [Security](../explanation/security.md) says what each right allows.
