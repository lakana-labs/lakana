#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Keep layers apart". The page shows the regions; the lines between
# regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local instance and
# sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the private home the runner sets.
cd "$WORK"
# The page shows tables as a terminal of 100 columns draws them.
export COLUMNS=100

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

# A project with the medallion default: bronze readings, cleaned in silver, summed in gold.
cd ~
lakana init plant --no-sample >/dev/null
cd plant
cp -R "$EXAMPLE_DIR/project/." .
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init -q
git add -A
git commit -q -m "Readings in three layers"
lakana repo add plant --kind local --url "$PWD" >/dev/null

capture_start
# docs:start dry-run
lakana deploy --env dev --dry-run
# docs:end dry-run
capture_end
grep -Eq '^Dry run of plant in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'hint: layer rules: 3 assets follow them (bronze 1, silver 1, gold 1)' "$captured"

# Build the gold table straight from bronze: the dry run refuses it, naming the rule and the fix.
cp "$EXAMPLE_DIR/refused/assets/gold/meter_usage.sql" assets/gold/meter_usage.sql
git add -A
git commit -q -m "Gold from bronze"
trap - ERR
set +e
capture_start
# docs:start refused
lakana deploy --env dev --dry-run
# docs:end refused
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
# The terminal wraps the message; join its lines before looking for it.
tr -s '\n ' ' ' <"$captured" | grep -Fq 'assets/gold/meter_usage.sql:7: `gold.meter_usage` reads `bronze.readings`, which is in bronze, but layers.gold.reads allows only silver and gold. Read the data from silver or gold instead, or add bronze to layers.gold.reads in lakana.yaml'

# Read silver again, as the rule says: the deploy passes.
cp "$EXAMPLE_DIR/project/assets/gold/meter_usage.sql" assets/gold/meter_usage.sql
git add -A
git commit -q -m "Gold from silver"
capture_start
# docs:start fixed
lakana deploy --env dev
# docs:end fixed
capture_end
grep -Eq '^Deployed plant in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'hint: layer rules: 3 assets follow them (bronze 1, silver 1, gold 1)' "$captured"
