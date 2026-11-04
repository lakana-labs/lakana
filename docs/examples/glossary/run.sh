#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Keep a glossary". The page shows the regions; the
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

# A project with a fact table, a dimension and a glossary of two terms.
cd ~
lakana init shop --no-sample >/dev/null
cd shop
cp -R "$EXAMPLE_DIR/project/." .
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init -q
git add -A
git commit -q -m "Sales, with a glossary"
lakana repo add shop --kind local --url "$PWD" >/dev/null

capture_start
# docs:start deploy
lakana deploy --env dev
# docs:end deploy
capture_end
grep -Eq '^Deployed shop in dev at [0-9a-f]{40}$' "$captured"
lakana run --env dev >/dev/null

# "revenue" is a synonym of the term, and the term's column is found by it too.
capture_start
# docs:start search
lakana search revenue --env dev
# docs:end search
capture_end
out=$(lakana --output json search revenue --env dev)
test "$(field "$out" '[(h["name"], h["matched"], h["term"]) for h in t["results"] if h["kind"] == "term"]')" = "[('net_revenue', 'synonym', 'net_revenue'), ('gross_revenue', 'name', 'gross_revenue')]"
test "$(field "$out" '[(h["matched"], h["term"]) for h in t["results"] if h["name"] == "gold.fact_sales.net_amount"]')" = "[('term', 'net_revenue')]"

# Read the term: its links, with the kind of each, and the terms it names and is named by.
capture_start
# docs:start term-get
lakana term get net_revenue --env dev
# docs:end term-get
capture_end
grep -Fxq '  gold.fact_sales.net_amount (measure)' "$captured"
grep -Fxq 'Named by:    (none)' "$captured"

# Read the table: the terms that point to it and to its columns.
capture_start
# docs:start table-get
lakana table get gold.fact_sales --env dev
# docs:end table-get
capture_end
grep -Fxq '  net_amount: net_revenue — Net revenue' "$captured"

# A misspelt column refuses the deploy, with the fix, and nothing changes.
cp "$EXAMPLE_DIR/broken/glossary/sales.yaml" glossary/sales.yaml
git commit -qam "A typo in the glossary"
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
tr -s '\n ' ' ' <"$captured" | grep -Fq 'net_revenue links gold.fact_sales column net_amout, which gold.fact_sales does not have (columns: date, store_id, gross_amount, net_amount); fix the column name or remove the link.'
git checkout -q HEAD~1 -- glossary/sales.yaml
git commit -qam "Fix the typo"
lakana deploy --env dev >/dev/null
