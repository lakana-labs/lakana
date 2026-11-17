#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Save and share a query". The page shows the regions; the lines
# between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK. The local instance's administrator owns what it saves
# and may run queries in "dev".
cd "$WORK"
export COLUMNS=120

lakana ns create gold.sales --env dev
lakana table create gold.sales.orders --column id:long --column 'status:string?' --env dev

field() {
  # field JSON_TEXT PYTHON_EXPRESSION_OF_q: print one value of a saved query's JSON.
  python3 -c 'import json,sys; q=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"
}

# docs:start create
lakana query saved create "Orders by status" "SELECT status, count(*) FROM gold.sales.orders GROUP BY status" --env dev
# docs:end create

out=$(lakana --output json query saved list --env dev)
test "$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$out")" = "1"
id=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0]["id"])' "$out")
one=$(lakana --output json query saved show "$id" --env dev)
test "$(field "$one" 'q["name"]')" = "Orders by status"
test "$(field "$one" 'q["visibility"]')" = "private"
test "$(field "$one" 'q["actions"]')" = "['update', 'delete']"

# docs:start share
lakana query saved update "$id" --env dev --everyone
# docs:end share

# The command read the query first and kept the rest.
one=$(lakana --output json query saved show "$id" --env dev)
test "$(field "$one" 'q["visibility"]')" = "everyone"
test "$(field "$one" 'q["sql"]')" = "SELECT status, count(*) FROM gold.sales.orders GROUP BY status"

# docs:start update
lakana query saved update "$id" --env dev --name "Orders by state"
# docs:end update

one=$(lakana --output json query saved show "$id" --env dev)
test "$(field "$one" 'q["name"]')" = "Orders by state"
test "$(field "$one" 'q["visibility"]')" = "everyone"

# docs:start list
lakana query saved list --env dev
lakana query saved list --all-environments
# docs:end list

all=$(lakana query saved list --all-environments)
case "$all" in *"environment"*"Orders by state"*) ;; *) echo "no environment column: $all" >&2; exit 1 ;; esac

# docs:start show-sql-run
lakana query saved show "$id" --env dev --sql | lakana sql - --env dev
# docs:end show-sql-run

run=$(lakana query saved show "$id" --env dev --sql | lakana sql - --env dev)
case "$run" in *status*) ;; *) echo "the saved SQL did not run: $run" >&2; exit 1 ;; esac

# docs:start delete
lakana query saved delete "$id" --env dev --yes
# docs:end delete

out=$(lakana --output json query saved list --env dev)
test "$out" = "[]"
