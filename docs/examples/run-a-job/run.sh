#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Run a job on a schedule", in order. The page shows the regions;
# the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the
# local instance (start.sh) and sets EXAMPLE_DIR and WORK.
cd "$WORK"
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

cd ~
lakana init readings --no-sample >/dev/null
cd readings
cp -R "$EXAMPLE_DIR/project/assets/." assets/
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# docs:start declare
cat "$EXAMPLE_DIR/project/jobs.yaml" >> lakana.yaml
# docs:end declare
git init -q
git add -A
git commit -q -m "Readings and two jobs"
lakana repo add readings --kind local --url "$PWD" >/dev/null

capture_start
# docs:start dry-run
lakana deploy --env dev --dry-run
# docs:end dry-run
capture_end
grep -Eq '^Dry run of readings in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'Jobs:' "$captured"
grep -Fq 'job `daily_silver` has a schedule but no run principal is bound in `dev`; it would not run' "$captured"

capture_start
# docs:start deploy
lakana deploy --env dev
# docs:end deploy
capture_end
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"

capture_start
# docs:start list-unbound
lakana job list --env dev
# docs:end list-unbound
capture_end
grep -Eq '^│ daily_silver +│ no run principal +│' "$captured"
grep -Eq '^│ hourly_bronze +│ no schedule +│' "$captured"

capture_start
# docs:start bind
lakana principal create etl-dev --type service-account
lakana grant create --principal etl-dev --role DATA_ENGINEER --kind ENVIRONMENT --env dev
lakana deployments set-run-as readings --env dev --principal etl-dev
# docs:end bind
capture_end

capture_start
# docs:start list
lakana job list --env dev
# docs:end list
capture_end
grep -Eq '^│ daily_silver +│ scheduled +│' "$captured"

capture_start
# docs:start pause
lakana job pause daily_silver --env dev
# docs:end pause
capture_end
grep -Fq "Paused daily_silver in dev; resume it with 'lakana job resume daily_silver --env dev'." "$captured"

capture_start
# docs:start resume
lakana job resume daily_silver --env dev
# docs:end resume
capture_end
grep -Eq '^Resumed daily_silver in dev; next run ' "$captured"

capture_start
# docs:start run
lakana job run hourly_bronze --env dev
# docs:end run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"

capture_start
# docs:start runs
lakana --output json runs list --env dev --limit 1
# docs:end runs
capture_end
grep -Fq '"job": "hourly_bronze"' "$captured"
grep -Fq '"trigger": "manual"' "$captured"

# The history of one task across the runs of its job, with what each attempt used.
capture_start
# docs:start task-history
lakana task history hourly_bronze bronze.readings --env dev --last 10
# docs:end task-history
capture_end
grep -Eq '^┃ run +┃ attempt +┃ status +┃ duration +┃ resources +┃' "$captured"
grep -Eq '^│ [0-9a-f-]{36} +│ 1 +│ succeeded +│ [0-9.]+s +│ ' "$captured"

# The pipeline's account: a service account that may start jobs, and its token. The token is
# shown once, so the commands the page shows run in a capture and a second token is issued
# for this script to use.
capture_start
# docs:start ci-account
lakana principal create ci-jobs --type service-account
lakana token issue ci-jobs github-jobs
lakana grant create --principal ci-jobs --action RUN --kind ENVIRONMENT --env dev
# docs:end ci-account
capture_end
grep -Fq 'Created service-account ci-jobs' "$captured"
ci_token=$(lakana --output json token issue ci-jobs script | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
ci_url=$(lakana --output json profile list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["url"])')

# In the pipeline LAKANA_URL and LAKANA_TOKEN come from CI secrets; here they are set the same way.
export LAKANA_URL="$ci_url" LAKANA_TOKEN="$ci_token"
capture_start
# docs:start ci-run
lakana job run daily_silver --env dev --no-wait
# docs:end ci-run
capture_end
unset LAKANA_URL LAKANA_TOKEN
capture_start
lakana --output json runs list --env dev --limit 1
capture_end
grep -Fq '"job": "daily_silver"' "$captured"
grep -Fq '"trigger": "api"' "$captured"
grep -Fq '"triggered_by": "ci-jobs"' "$captured"
