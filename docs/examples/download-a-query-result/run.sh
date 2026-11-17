#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Download a query result". The page shows the regions; the lines
# between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=100

# docs:start sql-out
lakana sql "SELECT 1 AS n, 'a,b' AS s" --env dev --out one.xlsx
# docs:end sql-out

# A spreadsheet is a zip file: it starts with "PK".
test "$(head -c 2 one.xlsx)" = "PK"

# docs:start result-csv
id=$(lakana --output json query submit "SELECT 1 AS n, 'a,b' AS s" --env dev | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
lakana query wait "$id" --env dev
lakana query result "$id" --env dev --format csv --out one.csv
# docs:end result-csv

# Every text value and header is quoted, rows end with CRLF, and the comma stays inside its value.
printf '"n","s"\r\n1,"a,b"\r\n' > expected.csv
cmp one.csv expected.csv

# docs:start list
lakana query list --all-environments --origin written --since 2026-01-01 --all
# docs:end list

out=$(lakana --output json query list --all-environments --origin written --since 2026-01-01 --all)
python3 -c '
import json, sys
rows = json.loads(sys.argv[1])
assert len(rows) >= 2, rows
assert all(r["origin"] == "written" and r["environment"] == "dev" for r in rows), rows
' "$out"
