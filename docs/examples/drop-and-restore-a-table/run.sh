#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Drop and restore a table". The page shows the regions; the lines
# between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

lakana table create silver.orders --column id:long --column 'status:string?'

uuid() {
  # uuid JSON_TEXT: print the `uuid` of `lakana table get` output.
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])["uuid"])' "$1"
}

before=$(uuid "$(lakana --output json table get silver.orders)")

# docs:start drop
lakana table delete silver.orders --yes
# docs:end drop

if lakana table get silver.orders >/dev/null 2>&1; then
  echo "the dropped table is still there" >&2
  exit 1
fi

# docs:start list
lakana table list silver --dropped
# docs:end list

out=$(lakana --output json table list silver --dropped)
python3 -c '
import json, sys
(entry,) = json.loads(sys.argv[1])
assert entry["name"] == "orders" and entry["restorable"] is True, entry
assert entry["purge_after"] > entry["dropped_at"], entry
' "$out"

# docs:start undrop
lakana table undrop silver.orders
# docs:end undrop

test "$(uuid "$(lakana --output json table get silver.orders)")" = "$before"
test "$(lakana --output json table list silver --dropped)" = "[]"

lakana table delete silver.orders --yes
lakana table create silver.orders --column id:long

# docs:start as
lakana table undrop silver.orders --as orders_before
# docs:end as

test "$(uuid "$(lakana --output json table get silver.orders_before)")" = "$before"

# docs:start purge
lakana table delete silver.orders_before --purge --yes
# docs:end purge

if message=$(lakana table undrop silver.orders_before 2>&1); then
  echo "a purged table was restored" >&2
  exit 1
fi
message=$(printf '%s' "$message" | tr -s '\n ' '  ')
case "$message" in
  *"undo period ended"*"--purge cannot be restored"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac

# Deleting and restoring an environment, on an environment of its own.
mkdir -p "$WORK/sandbox"
lakana env create sandbox --storage-root "file://$WORK/sandbox"
lakana table create silver.events --column id:long --env sandbox
events=$(uuid "$(lakana --output json table get silver.events --env sandbox)")

# docs:start env-delete
lakana env delete sandbox --yes
# docs:end env-delete

if lakana env get sandbox >/dev/null 2>&1; then
  echo "the deleted environment is still there" >&2
  exit 1
fi

# docs:start env-list-deleted
lakana env list --deleted
# docs:end env-list-deleted

out=$(lakana --output json env list --deleted)
python3 -c '
import json, sys
(entry,) = json.loads(sys.argv[1])
assert entry["name"] == "sandbox" and entry["purge_after"] > entry["deleted_at"], entry
' "$out"

# docs:start env-restore
lakana env restore sandbox
# docs:end env-restore

test "$(uuid "$(lakana --output json table get silver.events --env sandbox)")" = "$events"
test "$(lakana --output json env list --deleted)" = "[]"

# docs:start env-purge
lakana env delete sandbox --purge --yes
# docs:end env-purge

if message=$(lakana env restore sandbox 2>&1); then
  echo "a purged environment was restored" >&2
  exit 1
fi
message=$(printf '%s' "$message" | tr -s '\n ' '  ')
case "$message" in
  *"undo period ended"*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac

# Cleaning up a table directory an older release left without a record ("Clean up files left by
# older releases"). The directory is made by hand here, 40 days old, as an old release left it.
mkdir -p "$WORK/legacy"
lakana env create legacy --storage-root "file://$WORK/legacy"
leftover="$WORK/legacy/managed/gold/orders-0123456789abcdef0123456789abcdef"
mkdir -p "$leftover/data"
printf 'old' > "$leftover/data/00000.parquet"
touch -d '40 days ago' "$leftover/data/00000.parquet"
fresh="$WORK/legacy/managed/gold/events-fedcba9876543210fedcba9876543210"
mkdir -p "$fresh/data"
printf 'new' > "$fresh/data/00000.parquet"

# docs:start orphaned-list
lakana env orphaned-locations legacy
# docs:end orphaned-list

out=$(lakana --output json env orphaned-locations legacy)
python3 -c '
import json, sys
found = json.loads(sys.argv[1])
recent = {entry["location"]: entry["recent"] for entry in found["locations"]}
assert recent == {
    "managed/gold/orders-0123456789abcdef0123456789abcdef": False,
    "managed/gold/events-fedcba9876543210fedcba9876543210": True,
}, found
' "$out"

# docs:start orphaned-delete
lakana env orphaned-locations legacy --older-than 30d --delete --yes
# docs:end orphaned-delete

test ! -e "$leftover/data/00000.parquet"
test -e "$fresh/data/00000.parquet"

# A table that a deployed project builds: delete is refused, the orphan can be dropped after the
# asset is removed, and a rebuild drops the table and queues a run that builds it again.
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
cd "$WORK"
lakana init analytics --no-sample >/dev/null
cd analytics
mkdir -p assets/silver
printf '%s\n' '/* lakana' 'description: A table that the project builds.' '*/' 'SELECT 1 AS id' > assets/silver/clean.sql
printf '%s\n' '/* lakana' 'description: A table that stays.' '*/' 'SELECT 1 AS id' > assets/silver/stays.sql
git init -q
git add -A
git commit -q -m "A table the project builds"
lakana repo add analytics --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

set +e
lakana table delete silver.clean --env dev --yes
status=$?
set -e
test "$status" -ne 0
lakana table get silver.clean --env dev >/dev/null

git rm -q assets/silver/clean.sql
git commit -q -m "Stop building the table"
lakana deploy --env dev >/dev/null

# docs:start drop-orphan
lakana orphans --env dev
lakana table delete silver.clean --env dev --yes
# docs:end drop-orphan

if lakana table get silver.clean --env dev >/dev/null 2>&1; then
  echo "the dropped orphan is still there" >&2
  exit 1
fi

lakana table undrop silver.clean --env dev >/dev/null
git checkout -q HEAD~1 -- assets/silver/clean.sql
git commit -q -m "Build the table again"
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null

# docs:start rebuild
lakana table rebuild silver.clean --env dev --reason "history columns lost after a manual replace" --yes
# docs:end rebuild

for _ in $(seq 1 60); do
  if lakana runs list --env dev --status succeeded 2>/dev/null | grep -q succeeded; then
    sleep 1
    break
  fi
  sleep 1
done
lakana table get silver.clean --env dev >/dev/null
