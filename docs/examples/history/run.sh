#!/usr/bin/env bash
set -euo pipefail
# The reference "Keeping the history of a dimension": a history dimension run twice over a source
# that changes in between (Grace moves, Edsger is new, Ada is gone), a fact whose reference names
# the order's time, and a deploy that refuses a reference without it. The page shows the files and
# the regions; the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which
# starts the local instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=120

git config --global user.name "Your Name"
git config --global user.email "you@example.com"

lakana init crm --no-sample >/dev/null
cd crm
cp -R "$EXAMPLE_DIR/project/." .
git init -q
git add -A
git commit -q -m "A customer dimension that keeps its history"
lakana repo add crm --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

versions() {
  lakana --output json sql "SELECT customer_id, region, CAST(valid_from AS DATE) AS valid_from, is_current, valid_to IS NOT NULL AS closed FROM gold.dim_customer ORDER BY customer_id, valid_from" --env dev
}
out=$(versions)
python3 -c '
import json, sys
rows = [(r["customer_id"], r["region"], r["is_current"]) for r in json.loads(sys.argv[1])]
assert rows == [("C-001", "north", True), ("C-002", "north", True)], rows
' "$out"

cp "$EXAMPLE_DIR/changes/customers.sql" assets/silver/customers.sql
git commit -q -am "Grace moves, Edsger is new, Ada is gone"
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

# docs:start versions
lakana sql "SELECT customer_id, name, region, CAST(valid_from AS DATE) AS valid_from, is_current, valid_to IS NOT NULL AS closed FROM gold.dim_customer ORDER BY customer_id, valid_from" --env dev
# docs:end versions
out=$(versions)
python3 -c '
import json, sys
rows = [(r["customer_id"], r["region"], str(r["valid_from"]), r["is_current"], r["closed"]) for r in json.loads(sys.argv[1])]
assert rows == [
    ("C-001", "north", "0001-01-01", False, True),
    ("C-002", "north", "0001-01-01", False, True),
    ("C-002", "south", "2026-02-01", True, False),
    ("C-003", "west", "0001-01-01", True, False),
], rows
' "$out"

# Every order finds the version of its customer valid when it was placed.
check_outcome() {
  lakana --output json check list --env dev --table gold.fact_orders \
    | python3 -c 'import json,sys; print([c["outcome"] for c in json.load(sys.stdin) if c["check"] == "ref_customer_id"][0])'
}
test "$(check_outcome)" = "passed"

# A reference to a table that keeps history must say which version each row means.
cp "$EXAMPLE_DIR/refused/fact_orders.sql" assets/gold/fact_orders.sql
git commit -q -am "A reference without its time"
set +e
# docs:start refused
lakana deploy --env dev --dry-run
# docs:end refused
status=$?
set -e
test "$status" -ne 0
out=$(lakana deploy --env dev --dry-run 2>&1 || true)
# The terminal wraps the message; join its lines before looking for it.
tr -s '\n ' ' ' <<<"$out" | grep -Fq 'which keeps the history of its rows (`write: history`); say which version each row means'
git checkout -q HEAD~1 -- assets/gold/fact_orders.sql
git commit -q -am "The reference says its time again"

# A key's first version reaches back to the earliest time: an order dated before the run that
# first saw its customer still finds that version, so the reference check passes.
cp "$EXAMPLE_DIR/changes/orders.sql" assets/silver/orders.sql
git commit -q -am "An order from before the first run"
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null
test "$(check_outcome)" = "passed"
