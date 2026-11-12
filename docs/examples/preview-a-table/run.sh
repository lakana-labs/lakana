#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Preview a table". The page shows the regions; the lines between
# regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local instance
# and sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=100

lakana table create silver.orders --column order_id:long --column 'status:string?'
printf 'order_id,status\n1,open\n2,shipped\n3,cancelled\n' > orders.csv
lakana table append silver.orders orders.csv

# docs:start preview
lakana table preview silver.orders
# docs:end preview

out=$(lakana table preview silver.orders 2>&1)
case "$out" in
  *shipped*"First 3 rows"*"read as you"*) ;;
  *) echo "unexpected preview: $out" >&2; exit 1 ;;
esac
# Nothing was masked for the administrator, so no line says so.
case "$out" in
  *"Masked for you"*) echo "a preview without a mask must not name one" >&2; exit 1 ;;
esac

# docs:start json
lakana --output json table preview silver.orders
# docs:end json

out=$(lakana --output json table preview silver.orders)
python3 -c '
import json, sys
page = json.loads(sys.argv[1])
assert page["row_count"] == 3, page
assert page["preview_table"] == "silver.orders", page
assert page["masked_columns"] == [], page
assert page["rows"][1] == ["2", "shipped"] or page["rows"][1] == [2, "shipped"], page
assert page["next_offset"] is None, page
' "$out"

# An empty table says so.
lakana table create silver.empty --column id:long
out=$(lakana table preview silver.empty)
test "$out" = "No rows."
