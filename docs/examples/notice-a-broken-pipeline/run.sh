#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Notice a broken pipeline", in order. The page shows the regions; the
# lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the
# local instance (start.sh) and sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the
# private home the runner sets.
cd "$WORK"
# The page shows tables as a terminal of 120 columns draws them.
export COLUMNS=120

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
trap '[ -z "$capturing" ] || { exec >&3 2>&4; cat "$captured"; }; rm -f "$captured"' EXIT

# The id of the newest run of the environment `dev`.
latest_run() {
  lakana --output json runs list --env dev --limit 1 \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'
}

# The run's status, and the state and message of one task, as JSON the checks below read.
task_of() {
  lakana --output json runs show "$1" --env dev \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); t=[x for x in r["tasks"] if x["asset"]==sys.argv[1]][0]; print(json.dumps({"run": r["status"], "task": t["status"], "message": t.get("error_message") or ""}))' "$2"
}

# The number of columns of silver.orders, counted in its first row.
column_count() {
  lakana --output json sql "SELECT * FROM silver.orders ORDER BY id LIMIT 1" \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin)[0]))'
}

# A project of two assets and one job, as `lakana init` writes the folders. The files the page
# calls orders-v2.sql and orders-v3.sql are the next versions of silver/orders.sql: they are
# copied over it one at a time and are not assets of their own.
cd ~
lakana init readings --no-sample >/dev/null
cd readings
cp -R "$EXAMPLE_DIR/project/." .
rm assets/silver/orders-v2.sql assets/silver/orders-v3.sql
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init -q
git add -A
git commit -q -m "Orders, and a job that builds silver"
lakana repo add readings --kind local --url "$PWD" >/dev/null

capture_start
# docs:start deploy-and-run
lakana deploy --env dev
lakana job run nightly --env dev
# docs:end deploy-and-run
capture_end
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
test "$(column_count)" = 2

# Three runs are too few for a band, so volume has no history yet; schema needs none.
capture_start
# docs:start health
lakana table health silver.orders --env dev
# docs:end health
capture_end
out=$(lakana --output json table health silver.orders --env dev)
python3 -c 'import json,sys; s={x["signal"]: x["state"] for x in json.loads(sys.argv[1])["signals"]}; assert s["schema"] == "ok" and s["volume"] == "no_history", s' "$out"

# The change: a column is added, the schema signal fires, and it is told to block. The run fails,
# so the command's exit status is not zero: the lines around the region let the script go on and
# test the status.
cp "$EXAMPLE_DIR/project/assets/silver/orders-v2.sql" assets/silver/orders.sql
trap - ERR
set +e
capture_start
# docs:start change
git add -A
git commit -q -m "Add the amount to silver.orders"
lakana deploy --env dev
lakana job run nightly --env dev
# docs:end change
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"
grep -Eq '^Run [0-9a-f-]{36} failed$' "$captured"
RUN2=$(latest_run)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["run"] == "failed" and d["task"] == "failed", d; m=d["message"]; assert m.startswith("Check `silver.orders/health_schema` failed: health signal schema fired at snapshot "), m' "$(task_of "$RUN2" silver.orders)"
# The table keeps its previous data: two columns.
test "$(column_count)" = 2

# The history shows the firing, with the values the readers who may see them are shown.
capture_start
# docs:start signals
lakana table health silver.orders --env dev --signals --signal schema
# docs:end signals
capture_end
out=$(lakana --output json table health silver.orders --env dev --signals --signal schema)
python3 -c 'import json,sys; i=json.loads(sys.argv[1])["items"][0]; assert i["state"] == "fired" and i["set_back"] is True and i["observed"]["added"] == [["amount", "decimal(3, 1)"]], i' "$out"

# Let the change through: the same column, warned instead of blocked. The signal compares with the
# snapshot the table was set back to, so it fires again, and now it only warns.
cp "$EXAMPLE_DIR/project/assets/silver/orders-v3.sql" assets/silver/orders.sql
capture_start
# docs:start let-it-through
git add -A
git commit -q -m "Let the amount through"
lakana deploy --env dev
lakana job run nightly --env dev
# docs:end let-it-through
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
test "$(column_count)" = 3
out=$(lakana --output json table health silver.orders --env dev --signals --signal schema)
python3 -c 'import json,sys; i=json.loads(sys.argv[1])["items"][0]; assert i["state"] == "fired" and i["effect"] == "warn" and not i["set_back"], i' "$out"
# `schema: warn` is the default, so `table get` lists no effect.
capture_start
lakana table get silver.orders --env dev
capture_end
! grep -Fq 'Health (not the default warn)' "$captured"
