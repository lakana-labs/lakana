#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Register files as an external table". The page shows the regions;
# the lines between regions put the files in place and check what the page says. Run by
# scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

storage_dir() {
  # storage_dir ENV: print the local directory of an environment's storage root.
  lakana --output json env get "$1" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["storage_root"].removeprefix("file://"))'
}

refused() {
  # refused SENTENCE COMMAND...: run a command that must fail with SENTENCE in its message.
  local sentence=$1 message
  shift
  if message=$("$@" 2>&1); then
    echo "accepted, but should be refused: $*" >&2
    exit 1
  fi
  message=$(printf '%s' "$message" | tr -s '\n ' '  ')
  case "$message" in
    *"$sentence"*) ;;
    *) echo "unexpected refusal of $*: $message" >&2; exit 1 ;;
  esac
}

# The files a source system dropped under raw/ in dev's storage.
orders="$(storage_dir dev)/raw/orders"
mkdir -p "$orders/archive"
printf 'order_id,amount\n1,19.90\n2,5.00\n' > "$orders/2026-11-01.csv"
printf 'order_id,amount\n3,7.50\n' > "$orders/2026-11-02.csv"
# A file in a directory below is not read: only the directory's own files match.
printf 'order_id,amount\n9,1.00\n' > "$orders/archive/2026-10-01.csv"

# docs:start register
lakana table create-external bronze.orders --location 'raw/orders/*.csv' --format csv --env dev
# docs:end register

# The table reads the directory's own files, not the one in archive/.
out=$(lakana --output json sql "SELECT order_id FROM bronze.orders ORDER BY order_id")
python3 -c '
import json, sys
assert [row["order_id"] for row in json.loads(sys.argv[1])] == [1, 2, 3], sys.argv[1]
' "$out"

# Looking under raw/: the folder's files and archive/, the folder in use by bronze.orders.
# docs:start ls
lakana raw ls raw/orders --env dev
# docs:end ls
out=$(lakana --output json raw ls raw/orders --env dev)
python3 -c '
import json, sys
entries = {e["name"]: e for e in json.loads(sys.argv[1])}
assert sorted(entries) == ["2026-11-01.csv", "2026-11-02.csv", "archive"], sys.argv[1]
assert entries["archive"]["kind"] == "folder", sys.argv[1]
assert entries["2026-11-01.csv"]["claimed_by"] == {"table": "bronze.orders"}, sys.argv[1]
' "$out"

# The location rules, with the sentences the page quotes.
refused "Location pattern 'data/orders/*.csv' must start with 'raw/'." \
  lakana table create-external bronze.returns --location 'data/orders/*.csv' --format csv --env dev
refused "Location pattern 'raw/*.csv' must have the shape 'raw/<dir...>/<glob>'." \
  lakana table create-external bronze.returns --location 'raw/*.csv' --format csv --env dev
refused "Location pattern 'raw/orders/**/*.csv' must not contain '**'." \
  lakana table create-external bronze.returns --location 'raw/orders/**/*.csv' --format csv --env dev
refused "That directory is already claimed by another table." \
  lakana table create-external bronze.late_orders --location 'raw/orders/late-*.csv' --format csv --env dev
refused "No file matches 'raw/returns/*.csv'." \
  lakana table create-external bronze.returns --location 'raw/returns/*.csv' --format csv --env dev

# A protected environment of its own, with the same files.
mkdir -p "$WORK/prod/raw/orders"
cp "$orders/2026-11-01.csv" "$WORK/prod/raw/orders/"
lakana env create prod --storage-root "file://$WORK/prod"
lakana env protect prod

refused "Environment 'prod' is protected" \
  lakana table create-external bronze.orders --location 'raw/orders/*.csv' --format csv --env prod

# docs:start protected
lakana table create-external bronze.orders --location 'raw/orders/*.csv' --format csv --env prod --break-glass --reason "The orders feed lands in prod first"
# docs:end protected

lakana --output json table get bronze.orders --env prod > /dev/null
