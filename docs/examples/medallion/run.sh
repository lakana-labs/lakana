#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Model a star schema". The page shows the regions; the lines between
# regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local instance and
# sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the private home the runner sets.
cd "$WORK"
export COLUMNS=100

git config --global user.name "Your Name"
git config --global user.email "you@example.com"

assert_rows() {
  # assert_rows SQL EXPECTED: the rows SQL returns, as JSON, equal the Python literal EXPECTED.
  local out
  out=$(lakana --output json sql "$1" --env dev)
  python3 -c 'import json, sys; rows = json.loads(sys.argv[1]); assert rows == eval(sys.argv[2]), rows' "$out" "$2"
}

# docs:start init
lakana init retail --template medallion
# docs:end init
cd retail
# The template writes exactly what the example's project folder holds.
diff -r --exclude=.github --exclude=.gitlab-ci.yml --exclude=pyproject.toml --exclude=tests "$EXAMPLE_DIR/project" . >/dev/null
git init -q
git add -A
git commit -q -m "A star schema from the medallion template"
lakana repo add retail --kind local --url "$PWD" >/dev/null

# Every asset follows the layer rules the template declares.
out=$(lakana deploy --env dev --dry-run 2>&1)
grep -Fxq 'hint: layer rules: 13 assets follow them (bronze 4, silver 4, gold 5)' <<<"$out"

# docs:start deploy
lakana deploy --env dev
lakana run --env dev
# docs:end deploy

# Silver cleaned what bronze delivered: sale 12 arrived twice, the text had stray spaces.
assert_rows "SELECT count(*) AS n FROM bronze.sales" "[{'n': 13}]"
assert_rows "SELECT count(*) AS n FROM silver.sales" "[{'n': 12}]"
assert_rows "SELECT name FROM silver.stores ORDER BY store_id" "[{'name': 'Harbour'}, {'name': 'Station'}, {'name': 'Airport'}]"
assert_rows "SELECT city FROM silver.customers WHERE customer_id = 'C-003'" "[{'city': 'Munich'}]"

# The star: every key and reference check the header generated passed.
assert_rows "SELECT count(*) AS n FROM gold.fact_sales" "[{'n': 12}]"
assert_rows "SELECT CAST(sum(gross_amount) AS DOUBLE) AS gross, CAST(sum(net_amount) AS DOUBLE) AS net FROM gold.fact_sales" "[{'gross': 252.0, 'net': 240.0}]"
lakana --output json check list --env dev --table gold.fact_sales | python3 -c '
import json, sys
rows = json.load(sys.stdin)
names = {c["check"] for c in rows}
assert {"key_unique", "ref_store_id", "ref_product_id", "ref_customer_id", "measures"} <= names, names
assert all(c["outcome"] == "passed" for c in rows), rows
'

# docs:start star
lakana sql "SELECT store, SUM(net_amount) AS net_amount FROM gold.sales_wide GROUP BY store ORDER BY store" --env dev
# docs:end star
assert_rows "SELECT store, CAST(SUM(net_amount) AS DOUBLE) AS net FROM gold.sales_wide GROUP BY store ORDER BY store" "[{'store': 'Airport', 'net': 118.0}, {'store': 'Harbour', 'net': 42.0}, {'store': 'Station', 'net': 80.0}]"

# A second run without a change opens no new customer version.
lakana run --env dev >/dev/null
assert_rows "SELECT count(*) AS n FROM gold.dim_customer" "[{'n': 3}]"
assert_rows "SELECT customer_city, count(*) AS sales FROM gold.sales_wide WHERE customer_id = 'C-002' GROUP BY customer_city" "[{'customer_city': 'Berlin', 'sales': 4}]"

# Grace moves to Hamburg on 6 October, at noon.
cp "$EXAMPLE_DIR/changes/customers.sql" assets/bronze/customers.sql
git commit -q -am "Grace moves to Hamburg"
# docs:start change
lakana deploy --env dev
lakana run --env dev
# docs:end change

# docs:start versions
lakana sql "SELECT customer_id, city, CAST(valid_from AS DATE) AS valid_from, is_current FROM gold.dim_customer ORDER BY customer_id, valid_from" --env dev
# docs:end versions
out=$(lakana --output json sql "SELECT customer_id, city, CAST(valid_from AS DATE) AS valid_from, is_current FROM gold.dim_customer ORDER BY customer_id, valid_from" --env dev)
python3 -c '
import json, sys
rows = [(r["customer_id"], r["city"], str(r["valid_from"]), r["is_current"]) for r in json.loads(sys.argv[1])]
assert rows == [
    ("C-001", "Hamburg", "0001-01-01", True),
    ("C-002", "Berlin", "0001-01-01", False),
    ("C-002", "Hamburg", "2026-10-06", True),
    ("C-003", "Munich", "0001-01-01", True),
], rows
' "$out"

# Each sale keeps the city its customer lived in on the day: two before the move, two after.
# docs:start joined
lakana sql "SELECT sale_id, sold_on, customer_city FROM gold.sales_wide WHERE customer_id = 'C-002' ORDER BY sale_id" --env dev
# docs:end joined
assert_rows "SELECT sale_id, customer_city FROM gold.sales_wide WHERE customer_id = 'C-002' ORDER BY sale_id" "[{'sale_id': 2, 'customer_city': 'Berlin'}, {'sale_id': 5, 'customer_city': 'Berlin'}, {'sale_id': 8, 'customer_city': 'Hamburg'}, {'sale_id': 11, 'customer_city': 'Hamburg'}]"

# What depends on a store's key.
# docs:start impact
lakana graph impact gold.dim_store --column store_id --env dev
# docs:end impact
out=$(lakana graph impact gold.dim_store --column store_id --env dev)
grep -Fq 'gold.fact_sales' <<<"$out"

# The health of the fact, as the reference "Table health" describes it.
# docs:start health
lakana table health gold.fact_sales --env dev
# docs:end health
out=$(lakana table health gold.fact_sales --env dev)
grep -Eq '^│ volume +│ +│ no_history +│ warn +│' <<<"$out"

# Gold may not read bronze.
cp "$EXAMPLE_DIR/refused/assets/gold/raw_sales.sql" assets/gold/raw_sales.sql
git add -A
git commit -q -m "Gold from bronze"
set +e
# docs:start refused
lakana deploy --env dev --dry-run
# docs:end refused
status=$?
set -e
test "$status" -ne 0
out=$(lakana deploy --env dev --dry-run 2>&1 || true)
tr -s '\n ' ' ' <<<"$out" | grep -Fq 'reads `bronze.sales`, which is in bronze, but layers.gold.reads allows only silver and gold'
