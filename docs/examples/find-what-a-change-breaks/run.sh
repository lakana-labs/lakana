#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Find what a change breaks". The page shows the regions; the
# lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the private home the
# runner sets.
cd "$WORK"
# The page shows tables as a terminal of 100 columns draws them.
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

# Two repositories: stores builds the dimension, sales the fact that references it.
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
for name in stores sales; do
  cd ~
  lakana init "$name" --no-sample >/dev/null
  cd "$name"
  cp -R "$EXAMPLE_DIR/project/$name/." .
  git init -q
  git add -A
  git commit -q -m "First version"
  lakana repo add "$name" --kind local --url "$PWD" >/dev/null
done

cd ~/stores
# docs:start deploy-stores
lakana deploy --env dev
lakana run --env dev
# docs:end deploy-stores
cd ~/sales
# docs:start deploy-sales
lakana deploy --env dev
lakana run --env dev
# docs:end deploy-sales

capture_start
# docs:start impact
lakana graph impact gold.dim_store --column store_id --env dev
# docs:end impact
capture_end
out=$(lakana --output json graph impact gold.dim_store --column store_id --env dev)
test "$(field "$out" 'sorted((n["kind"], n["reason"]) for n in t["nodes"] if n["kind"] in ("reference", "check", "term"))')" = "[('check', 'checks the key of gold.dim_store'), ('check', 'checks the key of gold.dim_store'), ('check', 'checks the reference gold.fact_sales.store_id'), ('reference', 'references gold.dim_store.store_id'), ('term', 'links gold.dim_store.store_id')]"

capture_start
# docs:start join-path
lakana graph join-path gold.fact_sales gold.dim_store --env dev
# docs:end join-path
capture_end
grep -Fq 'gold.fact_sales → gold.dim_store on store_id = store_id (many to one: fact → dimension)' "$captured"

capture_start
# docs:start lineage
lakana graph lineage gold.fact_sales --direction up --env dev
# docs:end lineage
capture_end
grep -q 'gold.dim_store' "$captured"

# Renaming the dimension's key removes store_id: the deploy goes through, and warns.
cd ~/stores
cp "$EXAMPLE_DIR/changed/stores/assets/gold/dim_store.sql" assets/gold/dim_store.sql
git commit -qam "Rename the store key"
capture_start
# docs:start warned
lakana deploy --env dev
# docs:end warned
capture_end
tr -s '\n ' ' ' <"$captured" | grep -Fq "warning: this deploy removes the column \`store_id\` of \`gold.dim_store\`, which other repositories' declarations or assets use; tell their owners, or keep it, and see everything it affects with: lakana graph impact --env dev gold.dim_store --column store_id — what uses it: check \`gold.fact_sales/ref_store_id\` (sales), glossary term \`store\` (sales), reference \`gold.fact_sales.store_id → gold.dim_store.store_id\` (sales)"
