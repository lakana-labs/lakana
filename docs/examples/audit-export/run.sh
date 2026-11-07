#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Export and keep the audit log". The page shows the regions; the
# lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the
# local instance (its administrator holds READ_AUDIT_LOG) and sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=120

# docs:start auditor
lakana principal create auditor --type service-account
lakana grant create --kind INSTANCE --principal auditor --action READ_AUDIT_LOG
lakana token issue auditor audit-export
# docs:end auditor
out=$(lakana --output json grant list)
python3 -c 'import json,sys; g=json.loads(sys.argv[1]); assert any(x["kind"] == "INSTANCE" and x["action"] == "READ_AUDIT_LOG" for x in g), g' "$out"

# docs:start export-all
lakana --output json audit list --all > audit-all.json
# docs:end export-all
python3 -c 'import json; e=json.load(open("audit-all.json")); assert e and all("event_type" in x and "detail" in x and "occurred_at" in x for x in e), e'

# docs:start export-period
lakana --output json audit list --since 2020-01-01 --until 2020-02-01 --all > audit-2020-01.json
# docs:end export-period
python3 -c 'import json; assert json.load(open("audit-2020-01.json")) == []'

cp "$EXAMPLE_DIR/export-month.sh" .
month=$(date -u +%Y-%m)
# docs:start month
./export-month.sh 2026-10
# docs:end month
python3 -c 'import json; assert json.load(open("audit-2026-10.json")) == []'
sha256sum -c audit-2026-10.json.sha256
./export-month.sh "$month"
python3 -c 'import json,sys; e=json.load(open("audit-" + sys.argv[1] + ".json")); assert e and all(x["occurred_at"].startswith(sys.argv[1]) for x in e), e' "$month"
sha256sum -c "audit-$month.json.sha256"

# docs:start check-count
python3 -c 'import json; print(len(json.load(open("audit-all.json"))), "events")'
# docs:end check-count

# docs:start one-event
lakana --output json audit list --event-type PRINCIPAL_CREATED --all
# docs:end one-event
out=$(lakana --output json audit list --event-type PRINCIPAL_CREATED --all)
python3 -c 'import json,sys; e=json.loads(sys.argv[1]); assert [x["detail"]["principal"] for x in e] == ["auditor", "admin"], e' "$out"

# docs:start one-page
lakana --output json audit list --limit 2
# docs:end one-page
out=$(lakana --output json audit list --limit 2)
python3 -c 'import json,sys; p=json.loads(sys.argv[1]); assert len(p["items"]) == 2 and p["next_cursor"], p' "$out"
