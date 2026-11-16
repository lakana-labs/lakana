#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to guide "Move a SQL project to Lakana", in order. The page shows the regions;
# the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the
# local instance and sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the private home the
# runner sets. The page quotes the result of two queries; the other commands print what the
# tutorial "Getting started" already explains, so only their effects are checked here.
cd "$WORK"
# The page shows tables as a terminal of 120 columns draws them.
export COLUMNS=120

# The status of the newest run of the environment `dev`.
latest_run_status() {
  lakana --output json runs list --env dev --limit 1 \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["status"])'
}

cd ~
# docs:start init
lakana init shop --no-sample
cd shop
# docs:end init
test -f lakana.yaml
# The files the page shows, as the reader writes them. `lakana init` made the empty folders.
cp -R "$EXAMPLE_DIR/project/." .
test "$(find assets -name '*.sql' | wc -l)" -eq 3
test "$(find checks -name '*.sql' | wc -l)" -eq 5

# The CSV files stay in git, next to the project. Loading them is a command, run once.
# docs:start load
lakana table create bronze.regions --column region_code:string --column region_name:string
lakana table append bronze.regions data/regions.csv
lakana table create bronze.orders --column order_id:long --column region_code:string \
  --column status:string --column amount_cents:long --column order_date:date
lakana table append bronze.orders data/orders-1.csv
# docs:end load
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.orders")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 6}], sys.argv[1]' "$out"
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.regions")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 3}], sys.argv[1]' "$out"

git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# docs:start commit
git init
git add -A
git commit -m "Orders, regions and five checks"
# docs:end commit

# docs:start dry-run
lakana repo add shop --kind local --url "$PWD"
lakana deploy --env dev --dry-run
# docs:end dry-run

# docs:start deploy
lakana deploy --env dev
# docs:end deploy

# docs:start run
lakana run --env dev
# docs:end run
test "$(latest_run_status)" = succeeded
out=$(lakana --output json sql "SELECT count(*) AS n FROM silver.orders_enriched")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 6}], sys.argv[1]' "$out"

# docs:start query
lakana sql "SELECT * FROM gold.revenue_by_region ORDER BY revenue_cents DESC"
# docs:end query
out=$(lakana --output json sql "SELECT * FROM gold.revenue_by_region ORDER BY revenue_cents DESC")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"region_name":"South","orders":2,"revenue_cents":4800},{"region_name":"North","orders":1,"revenue_cents":2500},{"region_name":"East","orders":1,"revenue_cents":1200}], sys.argv[1]' "$out"

# Two more orders arrive. The files are loaded, the project is not deployed again: a run reads
# the tables as they are, and the incremental asset adds only the two new orders.
# docs:start more-orders
lakana table append bronze.orders data/orders-2.csv
lakana run --env dev
# docs:end more-orders
test "$(latest_run_status)" = succeeded
out=$(lakana --output json sql "SELECT count(*) AS n FROM silver.orders_enriched")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 8}], sys.argv[1]' "$out"

# docs:start query-again
lakana sql "SELECT * FROM gold.revenue_by_region ORDER BY revenue_cents DESC"
# docs:end query-again
out=$(lakana --output json sql "SELECT * FROM gold.revenue_by_region ORDER BY revenue_cents DESC")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"region_name":"North","orders":2,"revenue_cents":6500},{"region_name":"South","orders":2,"revenue_cents":4800},{"region_name":"East","orders":2,"revenue_cents":2000}], sys.argv[1]' "$out"

# docs:start lineage
lakana table lineage gold.revenue_by_region --env dev
# docs:end lineage
out=$(lakana table lineage gold.revenue_by_region --env dev)
grep -q 'silver.orders_enriched' <<<"$out"
