#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Declare a table's model", and of "See a table's model" in the
# how-to "Describe a table". The pages show the regions; the lines between regions are the checks.
# Run by scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and
# WORK. It starts in WORK, a folder of the private home the runner sets.
cd "$WORK"
# The pages show tables as a terminal of 100 columns draws them.
export COLUMNS=100

field() {
  # field JSON_TEXT PYTHON_EXPRESSION_OF_t: print one value of a command's JSON output.
  python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"
}

# The page quotes the output of several commands. Each is run as it is shown; its output is
# written to a file between capture_start and capture_end, printed again afterwards, and checked.
captured=""
capturing=""
capture_start() {
  rm -f "$captured"
  captured=$(mktemp)
  capturing=yes
  exec 3>&1 4>&2 >"$captured" 2>&1
}
capture_end() {
  exec >&3 2>&4 3>&- 4>&-
  capturing=""
  cat "$captured"
}
# If a command fails inside a capture, still show what it printed. Always remove the file.
trap '[ -z "$capturing" ] || { exec >&3 2>&4; cat "$captured"; }; rm -f "$captured"' EXIT

# The id of the newest run of the environment `dev`.
latest_run() {
  lakana --output json runs list --env dev --limit 1 \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'
}

# The outcome of each check of a run, as `TABLE/CHECK=OUTCOME` lines in name order.
outcomes() {
  lakana --output json check list --env dev --run "$1" \
    | python3 -c 'import json,sys; print("\n".join(sorted(x["name"] + "=" + x["outcome"] for x in json.load(sys.stdin))))'
}

# A star of one fact and two dimensions, with the bronze tables they read. One sale is of a
# product that gold.dim_product does not list.
cd ~
lakana init shop --no-sample >/dev/null
cd shop
cp -R "$EXAMPLE_DIR/project/." .
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init -q
git add -A
git commit -q -m "A star"
lakana repo add shop --kind local --url "$PWD" >/dev/null

capture_start
# docs:start deploy
lakana deploy --env dev
# docs:end deploy
capture_end
grep -Eq '^Deployed shop in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'Checks:' "$captured"
grep -Fxq '  gold.fact_sales: key_not_null (generated), key_unique (generated), ref_sku (generated) (warn), ref_store_id (generated) (warn)' "$captured"
grep -Fxq '  gold.dim_product: key_not_null (generated), key_unique (generated)' "$captured"

# The reference check of the unknown product fails and only warns: the run succeeds.
capture_start
# docs:start run
lakana run --env dev
# docs:end run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^gold\.fact_sales +warned +4 of 4 checks passed$' "$captured"
grep -Eq '^  ref_sku \(generated\) +warned +1 row$' "$captured"
RUN_ID=$(latest_run)
test "$(lakana --output json runs show "$RUN_ID" --env dev | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')" = "succeeded"
expected='gold.dim_product/key_not_null=passed
gold.dim_product/key_unique=passed
gold.dim_store/key_not_null=passed
gold.dim_store/key_unique=passed
gold.fact_sales/key_not_null=passed
gold.fact_sales/key_unique=passed
gold.fact_sales/ref_sku=warned
gold.fact_sales/ref_store_id=passed'
test "$(outcomes "$RUN_ID")" = "$expected"

# The run's summary, with the checks of each table in name order.
capture_start
# docs:start show
lakana runs show "$RUN_ID" --env dev
# docs:end show
capture_end
grep -Eq '^gold\.fact_sales +warned +4 of 4 checks passed$' "$captured"
grep -Eq '^  ref_sku \(generated\) +warned +1 row$' "$captured"

capture_start
# docs:start check-list
lakana check list --env dev --table gold.fact_sales
# docs:end check-list
capture_end
grep -Eq '│ ref_sku \(generated\) +│ warned +│ 1 +│' "$captured"
grep -Eq '│ ref_store_id \(generated\) +│ passed +│' "$captured"

# The rows the reference check found: the sale of the product `soup`.
capture_start
# docs:start check-rows
lakana check rows "$RUN_ID" gold.fact_sales/ref_sku --env dev
# docs:end check-rows
capture_end
grep -Eq '│ soup +│' "$captured"
grep -Fxq '1 rows' "$captured"

# The fix: list the product, deploy and run again. Every check passes.
cp "$EXAMPLE_DIR/fixed/assets/bronze/products.sql" assets/bronze/products.sql
git add -A
git commit -q -m "List the soup"
lakana deploy --env dev >/dev/null
capture_start
# docs:start run-fixed
lakana run --env dev
# docs:end run-fixed
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^gold\.fact_sales +passed +4 of 4 checks passed$' "$captured"
test "$(outcomes "$(latest_run)" | grep -c '=passed$')" -eq 8

# docs:start get
lakana table get gold.fact_sales --env dev
# docs:end get

out=$(lakana --output json table get gold.fact_sales --env dev)
test "$(field "$out" 't["model"]["kind"]')" = "fact"
test "$(field "$out" 't["model"]["grain"]')" = "One row per store, product and calendar day."
test "$(field "$out" 't["model"]["key"]')" = "['date', 'store_id', 'sku']"
test "$(field "$out" '[(r["id"], r["target_exists"]) for r in t["model"]["references"]]')" = "[('gold.fact_sales/ref_sku', True), ('gold.fact_sales/ref_store_id', True)]"
test "$(field "$out" '[(c["check"], c["outcome"], c["generated"]) for c in t["model"]["checks"]]')" = "[('key_not_null', 'passed', True), ('key_unique', 'passed', True), ('ref_sku', 'passed', True), ('ref_store_id', 'passed', True)]"
out=$(lakana --output json table get bronze.sales --env dev)
test "$(field "$out" 't["model"]')" = "None"

# docs:start references
lakana table references gold.dim_store --env dev
# docs:end references

out=$(lakana --output json table references gold.dim_store --env dev)
test "$(field "$out" '[r["id"] for r in t["incoming"]]')" = "['gold.fact_sales/ref_store_id']"
out=$(lakana --output json table references gold.dim_product --env dev)
test "$(field "$out" '[r["id"] for r in t["incoming"]]')" = "['gold.fact_sales/ref_sku']"

# docs:start list
lakana table list gold --model-kind fact --env dev
# docs:end list

out=$(lakana --output json table list gold --model-kind fact --env dev)
test "$(field "$out" '[x["name"] for x in t]')" = "['fact_sales']"

# A kind no header can declare is refused with the four kinds.
if message=$(lakana table list gold --model-kind cube --env dev 2>&1); then
  echo "an unknown kind was accepted" >&2
  exit 1
fi
case "$message" in
  *"must be one of fact, dimension, wide, staging; got 'cube'"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac

# Search finds the fact by the words of its grain.
capture_start
# docs:start search
lakana search "calendar" --model-kind fact --env dev
# docs:end search
capture_end
out=$(lakana --output json search "calendar" --model-kind fact --env dev)
test "$(field "$out" '[(h["name"], h["matched"]) for h in t["results"]]')" = "[('gold.fact_sales', 'grain')]"

# A product listed twice: the dimension's key check blocks, so the fact that references it is not
# built. --upstream builds the dimension first because the fact references it.
cp "$EXAMPLE_DIR/broken/assets/bronze/products.sql" assets/bronze/products.sql
git add -A
git commit -q -m "List the soup twice"
lakana deploy --env dev >/dev/null
trap - ERR
set +e
capture_start
# docs:start upstream
lakana run --env dev --select gold.fact_sales --upstream
# docs:end upstream
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^Run [0-9a-f-]{36} failed$' "$captured"
grep -Eq '^│ gold\.dim_product +│ failed +│' "$captured"
grep -Eq '^│ gold\.fact_sales +│ upstream_failed +│' "$captured"
grep -Eq '^  key_unique \(generated\) +FAILED +2 rows$' "$captured"
cp "$EXAMPLE_DIR/fixed/assets/bronze/products.sql" assets/bronze/products.sql
git add -A
git commit -q -m "List the soup once"

# A dimension without a key is refused at deploy, and nothing changes.
cp "$EXAMPLE_DIR/refused/assets/gold/dim_product.sql" assets/gold/dim_product.sql
git add -A
git commit -q -m "A dimension without a key"
trap - ERR
set +e
capture_start
# docs:start refused
lakana deploy --env dev
# docs:end refused
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
# The terminal wraps the message; join its lines before looking for it.
tr -d '\n' <"$captured" | grep -Fq 'assets/gold/dim_product.sql:1: Asset `gold.dim_product` is a dimension and needs `key:`; `key: none` is not allowed for a dimension, because nothing could refer to it.'
cp "$EXAMPLE_DIR/project/assets/gold/dim_product.sql" assets/gold/dim_product.sql
git add -A
git commit -q -m "The dimension's key again"
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

# The fact declares its measure; the generated `measures` check passes because `amount` is a
# number column, which `aggregation: sum` needs.
cp "$EXAMPLE_DIR/measures/assets/gold/fact_sales.sql" assets/gold/fact_sales.sql
git add -A
git commit -q -m "Declare the measure"
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

# docs:start measures
lakana table get gold.fact_sales --env dev
# docs:end measures

out=$(lakana --output json table get gold.fact_sales --env dev)
test "$(field "$out" '[(m["id"], m["aggregation"], m["additivity"], m["currency"]) for m in t["model"]["measures"]]')" = "[('gold.fact_sales/measure_amount', 'sum', 'additive', 'EUR')]"
test "$(field "$out" '[c["outcome"] for c in t["model"]["checks"] if c["check"] == "measures"]')" = "['passed']"
out=$(lakana --output json search "Sales amount" --env dev)
test "$(field "$out" '[(h["name"], h["matched"]) for h in t["results"]]')" = "[('gold.fact_sales.amount', 'measure')]"
