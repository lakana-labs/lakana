#!/usr/bin/env bash
set -euo pipefail
# The commands of "Start a job after each deploy" in the how-to "Run a job on a schedule", in
# order. The page shows the regions; the lines between regions are the checks. Run by
# scripts/run_docs_examples.sh, which starts the local instance (start.sh) and sets EXAMPLE_DIR
# and WORK.
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
git commit -q -m "Readings and a job that runs after each deploy"
lakana repo add readings --kind local --url "$PWD" >/dev/null

capture_start
# docs:start deploy
lakana deploy --env dev
# docs:end deploy
capture_end
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'Jobs started after the deploy:' "$captured"
grep -Eq '^  refresh: run [0-9a-f-]{36}$' "$captured"

capture_start
# docs:start runs
lakana --output json runs list --env dev --trigger deploy --limit 1
# docs:end runs
capture_end
grep -Fq '"job": "refresh"' "$captured"
grep -Fq '"trigger": "deploy"' "$captured"

# Deploying the commit that is already current changes nothing, so it starts nothing.
capture_start
lakana deploy --env dev
capture_end
if grep -Fq 'Jobs started after the deploy:' "$captured"; then
  echo "a deploy of the current commit started a job" >&2
  exit 1
fi

# A deploy account without RUN: the deploy goes through, the job does not start, and the warning
# says why and what to do. Its token is issued for this script; in CI it comes from a secret.
capture_start
# docs:start ci-account
lakana principal create ci-deploy --type service-account
lakana grant create --principal ci-deploy --action DEPLOY --kind ENVIRONMENT --env dev
# docs:end ci-account
capture_end
grep -Fq 'Created service-account ci-deploy' "$captured"
ci_token=$(lakana --output json token issue ci-deploy script | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
ci_url=$(lakana --output json profile list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["url"])')
git commit -q --allow-empty -m "A change"

export LAKANA_URL="$ci_url" LAKANA_TOKEN="$ci_token"
capture_start
# docs:start ci-deploy
lakana deploy --env dev
# docs:end ci-deploy
capture_end
unset LAKANA_URL LAKANA_TOKEN
grep -Eq '^Deployed readings in dev at [0-9a-f]{40}$' "$captured"
grep -Fq 'warning: job `refresh` was not started: ci-deploy may not RUN in `dev`.' "$captured"
