#!/usr/bin/env bash
set -euo pipefail
# The write modes of the reference "Asset front-matter": a `write: merge` asset and a
# `write: append` asset, run twice over a source that changes in between, and the hint a deploy
# prints for the older spelling. The page shows the files and the regions; the lines between
# regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local instance and
# sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=120

git config --global user.name "Your Name"
git config --global user.email "you@example.com"

lakana init analytics --no-sample >/dev/null
cd analytics
cp -R "$EXAMPLE_DIR/project/." .
git init -q
git add -A
git commit -q -m "A merge and an append"
lakana repo add analytics --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

rows() {
  lakana --output json sql "SELECT id, name FROM $1 ORDER BY id, updated_at" --env dev
}
out=$(rows silver.customers)
python3 -c 'import json,sys; assert [r["id"] for r in json.loads(sys.argv[1])] == [1, 2], sys.argv[1]' "$out"
out=$(rows silver.customer_changes)
python3 -c 'import json,sys; assert [r["id"] for r in json.loads(sys.argv[1])] == [1, 2], sys.argv[1]' "$out"

# The source changes: Grace gets her full name, and Edsger is new.
cp "$EXAMPLE_DIR/changes/customer_updates.sql" assets/bronze/customer_updates.sql
git commit -q -am "Grace changes, Edsger is new"
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

# docs:start merged
lakana sql "SELECT id, name FROM silver.customers ORDER BY id" --env dev
# docs:end merged
out=$(rows silver.customers)
python3 -c '
import json, sys
rows = json.loads(sys.argv[1])
assert [(r["id"], r["name"]) for r in rows] == [(1, "Ada"), (2, "Grace Hopper"), (3, "Edsger")], rows
' "$out"

# docs:start appended
lakana sql "SELECT id, name FROM silver.customer_changes ORDER BY id, updated_at" --env dev
# docs:end appended
out=$(rows silver.customer_changes)
python3 -c '
import json, sys
rows = json.loads(sys.argv[1])
names = [(r["id"], r["name"]) for r in rows]
assert names == [(1, "Ada"), (2, "Grace"), (2, "Grace Hopper"), (3, "Edsger")], rows
' "$out"

# The older spelling still deploys; the deploy says how it is written now.
cp "$EXAMPLE_DIR/changes/orders_log.sql" assets/silver/orders_log.sql
git add -A
git commit -q -m "An asset in the older spelling"
# docs:start hint
lakana deploy --env dev --dry-run
# docs:end hint
out=$(lakana --output json deploy --env dev --dry-run)
python3 -c '
import json, sys
hints = json.loads(sys.argv[1])["diff"]["hints"]
assert hints == ["silver.orders_log: `materialize: incremental` is now written `write: append` with `watermark: order_id`; both work"], hints
' "$out"
