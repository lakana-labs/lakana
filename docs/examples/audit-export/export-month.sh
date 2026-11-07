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
