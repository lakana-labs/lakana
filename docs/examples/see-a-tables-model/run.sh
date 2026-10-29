#!/usr/bin/env bash
set -euo pipefail
# The commands of "See a table's model" in the how-to "Describe a table". The page shows the
# regions; the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which
# starts the local instance and sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the
# private home the runner sets.
cd "$WORK"
# The page shows tables as a terminal of 100 columns draws them.
export COLUMNS=100

field() {
  # field JSON_TEXT PYTHON_EXPRESSION_OF_t: print one value of a command's JSON output.
  python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"
}

# A star of one fact and one dimension, with the bronze tables they read, deployed and run.
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
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

# docs:start get
lakana table get gold.fact_sales
# docs:end get

out=$(lakana --output json table get gold.fact_sales)
test "$(field "$out" 't["model"]["kind"]')" = "fact"
test "$(field "$out" 't["model"]["key"]')" = "['date', 'store_id']"
test "$(field "$out" '[(r["id"], r["target_exists"]) for r in t["model"]["references"]]')" = "[('gold.fact_sales/ref_store_id', True)]"
test "$(field "$out" '[(c["check"], c["outcome"], c["generated"]) for c in t["model"]["checks"]]')" = "[('key_not_null', 'passed', True), ('key_unique', 'passed', True), ('ref_store_id', 'passed', True)]"
out=$(lakana --output json table get bronze.sales)
test "$(field "$out" 't["model"]')" = "None"

# docs:start references
lakana table references gold.dim_store
# docs:end references

out=$(lakana --output json table references gold.dim_store)
test "$(field "$out" '[r["id"] for r in t["incoming"]]')" = "['gold.fact_sales/ref_store_id']"

# docs:start list
lakana table list gold --model-kind fact
# docs:end list

out=$(lakana --output json table list gold --model-kind fact)
test "$(field "$out" '[x["name"] for x in t]')" = "['fact_sales']"

# A kind no header can declare is refused with the four kinds.
if message=$(lakana table list gold --model-kind cube 2>&1); then
  echo "an unknown kind was accepted" >&2
  exit 1
fi
case "$message" in
  *"model_kind must be one of fact, dimension, wide, staging; got 'cube'"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac
