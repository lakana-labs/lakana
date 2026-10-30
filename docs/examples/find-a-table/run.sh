#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Find a table". The page shows the regions; the lines between
# regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local instance
# and sets EXAMPLE_DIR and WORK.
cd "$WORK"

lakana table create silver.orders --column order_id:long --column 'status:string?'
lakana table describe silver.orders --text "One row per customer order."

names() {
  # names JSON_TEXT: print the kind and name of each hit, one per line.
  python3 -c 'import json,sys; print("\n".join(h["kind"] + " " + h["name"] for h in json.loads(sys.argv[1])["results"]))' "$1"
}

# docs:start search
lakana search order
# docs:end search

out=$(lakana --output json search order)
test "$(names "$out" | head -n 1)" = "table silver.orders"
names "$out" | grep -qx "column silver.orders.order_id"

# docs:start kind
lakana search customer --kind table
# docs:end kind

out=$(lakana --output json search customer --kind table)
test "$(names "$out")" = "table silver.orders"
test "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["results"][0]["matched"])' "$out")" = "description"

# docs:start json
lakana --output json search status --kind column
# docs:end json

out=$(lakana --output json search status --kind column)
test "$(names "$out")" = "column silver.orders.status"

# docs:start model-kind
lakana search store --model-kind dimension
# docs:end model-kind

# The sandbox table declares no kind, so the filter leaves nothing.
out=$(lakana --output json search order --model-kind dimension)
test "$(names "$out")" = ""
if lakana search order --model-kind cube 2>/dev/null; then echo "an unknown kind must be refused" >&2; exit 1; fi

# A text that matches nothing says what search looks at.
message=$(lakana search zzzzzz 2>&1 | tr -s '\n ' '  ')
case "$message" in
  *"No match"*"table names, column names"*) ;;
  *) echo "unexpected answer: $message" >&2; exit 1 ;;
esac
