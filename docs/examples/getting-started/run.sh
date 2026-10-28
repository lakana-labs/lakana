#!/usr/bin/env bash
set -euo pipefail
# The commands of the "Getting started" tutorial, in order. The page shows the regions;
# the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which
# starts the local instance (start.sh) and sets EXAMPLE_DIR and WORK. It starts in WORK, a
# folder of the private home the runner sets, as a reader starts in some folder of theirs.
cd "$WORK"

# The page quotes the output of the deploy and the run. The commands of those two regions are
# run as they are shown; their output is written to a file between capture_start and
# capture_end, printed again afterwards, and checked for the lines that do not change from
# one machine to the next (identifiers, commits and durations do).
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

# docs:start version
lakana --version
# docs:end version
# The instance is up, so both lines are printed: the CLI's version and the instance's.
lakana --version | grep -q '^instance http://'

# The first step loads a small CSV file into bronze. The reader saves the file the page shows in
# the folder they are in; here it is copied from the example into WORK.
cp "$EXAMPLE_DIR/readings.csv" .
capture_start
# docs:start load
lakana load readings.csv first_file --env dev --yes
# docs:end load
capture_end
grep -Fxq 'readings.csv: 3 rows, 3 columns' "$captured"
grep -Fxq '  day: date32[day]' "$captured"
grep -Fxq 'Loaded 3 rows into dev/bronze.first_file (created the table). To clean it up or load it again on a schedule, write an asset that reads it.' "$captured"
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.first_file")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 3}], sys.argv[1]' "$out"
# Any other layer is refused by the server, before a row is uploaded.
set +e
out=$(lakana load readings.csv silver.first_file --env dev --yes 2>&1)
status=$?
set -e
test "$status" -ne 0
tr -s '[:space:]' ' ' <<<"$out" | grep -Fq 'loads into bronze only: load into `bronze.first_file`'

capture_start
# docs:start init
cd ~
lakana init analytics
cd analytics
# docs:end init
capture_end
grep -Eq '^Created a Lakana project in .*analytics, with a small sample' "$captured"
# The query the init output suggests is the one the tutorial shows at the end.
grep -Fq 'lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"' "$captured"
grep -Fxq 'Then the two commands that build the sample:' "$captured"
grep -Eq "^  lakana run --env dev$" "$captured"
grep -Eq "^  lakana deploy --env dev$" "$captured"
# The hint appears exactly when git cannot name the author (git guesses one from the host name on
# some machines, macOS runners among them).
if git var GIT_AUTHOR_IDENT > /dev/null 2>&1 && git var GIT_COMMITTER_IDENT > /dev/null 2>&1; then
  if grep -Fq "If 'git commit' says it does not know who you are" "$captured"; then
    echo "lakana init printed the git identity hint although git knows the author" >&2
    exit 1
  fi
else
  grep -Fq "If 'git commit' says it does not know who you are" "$captured"
fi
test -f lakana.yaml

# `lakana init` wrote the sample the tutorial shows by its own paths: the files under project/
# are the ones it wrote (a unit test compares them with the CLI's templates; here the project
# itself is compared).
diff -r "$EXAMPLE_DIR/project/assets" assets
diff -r "$EXAMPLE_DIR/project/checks" checks
test "$(find assets -name '*.sql' | wc -l)" -eq 3
test "$(find checks -name '*.sql' | wc -l)" -eq 1

# docs:start git-identity
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# docs:end git-identity
test "$(git config --global user.email)" = "you@example.com"

# docs:start commit
git init
git add -A
git commit -m "Initial project"
# docs:end commit

capture_start
# docs:start deploy
lakana repo add analytics --kind local --url "$PWD"
lakana deploy --env dev
# docs:end deploy
capture_end
grep -q '^Registered repository analytics' "$captured"
grep -Fxq "Hint: run 'lakana run --env dev' to run what you just deployed." "$captured"
grep -Eq '^Deployed analytics in dev at [0-9a-f]{40}$' "$captured"
grep -Eq '^│ added +│ bronze\.readings +│$' "$captured"
grep -Eq '^│ added +│ gold\.station_ranking +│$' "$captured"
grep -Eq '^│ added +│ silver\.station_temperatures +│$' "$captured"
grep -Fxq 'Checks:' "$captured"
grep -Fxq '  silver.station_temperatures: plausible_range' "$captured"

capture_start
# docs:start run
lakana run --env dev
# docs:end run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^│ bronze\.readings +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 18 +│ +│$' "$captured"
grep -Eq '^│ gold\.station_ranking +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 3 +│ +│$' "$captured"
grep -Eq '^│ silver\.station_temperatures +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 3 +│ +│$' "$captured"
grep -Eq '^silver\.station_temperatures +passed +1 of 1 checks passed$' "$captured"
grep -Eq '^  plausible_range +passed$' "$captured"

# docs:start query
lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
# docs:end query
out=$(lakana --output json sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"station_id":"C3","avg_temp_c":9.0,"warmth_rank":1},{"station_id":"A1","avg_temp_c":4.0,"warmth_rank":2},{"station_id":"B2","avg_temp_c":1.0,"warmth_rank":3}], sys.argv[1]' "$out"
# The how-to "Stop a query" shows how to find a query, and "Deploy with Docker Compose" how to see
# the workers. Every query of this instance has ended by now, so the first listing needs --all;
# the second lists the one worker the local instance started.
# docs:start query-list
lakana query list --env dev --all
# docs:end query-list
out=$(lakana --output json query list --env dev --all)
python3 -c 'import json,sys; q=json.loads(sys.argv[1]); assert isinstance(q, list) and q and all(x["status"] == "SUCCEEDED" and not {"sql_text","error_message","lineage"} & set(x) for x in q), q' "$out"
out=$(lakana --output json query list --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"

# The how-to "Find a run, a query or a deploy" shows one filter per list. The run above succeeded
# and nothing failed or was cancelled, so a filter on `failed` finds nothing.
# docs:start find-failed-run
lakana runs list --env dev --status failed --since "$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)"
# docs:end find-failed-run
out=$(lakana --output json runs list --env dev --status failed --since "$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"
out=$(lakana --output json runs list --env dev --status succeeded,failed,cancelled --since 2020-01-01 --trigger manual)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"
out=$(lakana --output json runs list --env dev --status succeeded --since 2020-01-01 --limit 5)
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert len(r) == 1 and r[0]["status"] == "succeeded", r' "$out"
# docs:start find-person-query
lakana query list --env dev --status failed --submitter admin
# docs:end find-person-query
out=$(lakana --output json query list --env dev --status failed --submitter admin)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"
out=$(lakana --output json query list --env dev --status succeeded --submitter admin)
python3 -c 'import json,sys; q=json.loads(sys.argv[1]); assert q and all(x["submitter"] == "admin" for x in q), q' "$out"
# docs:start find-repo-deploys
lakana deploys list --env dev --repo analytics --limit 20
# docs:end find-repo-deploys
out=$(lakana --output json deploys list --env dev --repo analytics --limit 20)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert len(d) == 1 and d[0]["repository"] == "analytics", d' "$out"

# docs:start worker-list
lakana worker list
# docs:end worker-list
out=$(lakana --output json worker list)
python3 -c 'import json,sys; w=json.loads(sys.argv[1]); assert len(w) == 1 and w[0]["status"] == "alive" and w[0]["takes"] == "all" and w[0]["query_run_id"] is None, w' "$out"

# The how-to "Why is my query or task waiting?" names the three commands an operator looks at.
# docs:start scheduler-status
lakana scheduler status
# docs:end scheduler-status
out=$(lakana --output json scheduler status)
python3 -c 'import json,sys; s=json.loads(sys.argv[1]); assert set(s) == {"holder", "heartbeat_at", "active_runs", "pending", "ready", "running"} and s["active_runs"] == 0, s' "$out"

# The how-to "Is my instance healthy?" shows the health route, the queue and the open runs. The
# address is the one the profile holds, as an operator has it at hand. Every query and run of this
# instance has ended, so both listings are empty.
URL=$(lakana --output json profile list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["url"])')
# docs:start healthz
curl -s "$URL/healthz"
# docs:end healthz
out=$(curl -s "$URL/healthz")
test "$out" = '{"status":"ok"}'
# The worker above is alive and `dev`'s storage is this machine's disk, so the instance is ready.
# docs:start readyz
curl -s "$URL/readyz"
# docs:end readyz
out=$(curl -s "$URL/readyz")
test "$out" = '{"status":"ready"}'
# docs:start queue-length
lakana query list --env dev --status queued
# docs:end queue-length
out=$(lakana --output json query list --env dev --status queued)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"
# docs:start open-runs
lakana runs list --env dev --status queued,running
# docs:end open-runs
out=$(lakana --output json runs list --env dev --status queued,running)
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [], sys.argv[1]' "$out"
# The same how-to shows how much storage the environment and the workers' caches use. The tables
# may not be measured yet (the worker's sweep measures them), so only the shape is checked.
# docs:start env-usage
lakana env usage dev
# docs:end env-usage
out=$(lakana --output json env usage dev)
python3 -c 'import json,sys; u=json.loads(sys.argv[1]); e=u["environments"]; assert [x["environment"] for x in e] == ["dev"] and e[0]["tables"]["count"] >= 1 and len(u["workers"]) == 1 and "caches" in u["workers"][0], u' "$out"

# The how-to "Use a private package index" shows how an index and its secret are registered. That
# never contacts the index, and a local instance has a secrets master key, so the commands run
# as the page shows them (the index's address is the page's own example).
cd "$WORK"
printf '%s\n' '{"username": "ci-reader", "password": "the-password-or-token"}' > credential.json
# docs:start private-index-register
lakana secret create team-index --env dev --values-stdin < credential.json
lakana index add team --url https://pkgs.example.org/simple --credential-secret team-index
# docs:end private-index-register
out=$(lakana --output json secret list --env dev)
python3 -c 'import json,sys; s={x["name"]:(x["use"],x["use_target"]) for x in json.loads(sys.argv[1])}; assert s["team-index"] == ("package_index", "team"), s' "$out"
out=$(lakana --output json index show team)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["url"] == "https://pkgs.example.org/simple" and d["credential_secret"] == "team-index", d' "$out"
# The listing is read whole before it is searched: `grep -q` stops at the first match, and a
# command still writing into the closed pipe would fail the script under `pipefail`.
out=$(lakana secret list --env dev)
grep -q 'team-index' <<<"$out"

# docs:start private-index-rotate
lakana secret rotate team-index --env dev --values-stdin < credential.json
# docs:end private-index-rotate
out=$(lakana secret list --env dev)
grep -q 'team-index' <<<"$out"

# docs:start private-index-clear
lakana index update team --clear credential_secret
# docs:end private-index-clear
out=$(lakana --output json index show team)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["credential_secret"] is None, d' "$out"

# The upgrade note "Delegation needs the ADMIN role" tells an operator how to find who holds the
# ADMIN role and what each administrator created. A local instance has its bootstrap
# administrator (`admin`), so the commands run as the page shows them.
# docs:start delegation-check
lakana whoami
lakana principal list
lakana --output json grant list
lakana group list
# docs:end delegation-check
out=$(lakana --output json grant list)
python3 -c 'import json,sys; g=json.loads(sys.argv[1]); assert any(x["role"] == "ADMIN" and x["kind"] == "INSTANCE" and x["principal_id"] for x in g), g' "$out"

# docs:start delegation-audit
lakana --output json audit list --event-type GRANT_CREATED --all
lakana --output json audit list --event-type GROUP_MEMBER_ADDED --all
lakana --output json audit list --event-type PAT_ISSUED --all
# docs:end delegation-audit
out=$(lakana --output json audit list --event-type GRANT_CREATED --all)
python3 -c 'import json,sys; e=json.loads(sys.argv[1]); assert e and all(x["event_type"] == "GRANT_CREATED" and x["principal_id"] for x in e), e' "$out"

# The lists reference shows how to read who looked at other people's queries or a table's readers.
# docs:start view-audit
lakana audit list --event-type ACTIVITY_VIEWED
# docs:end view-audit
out=$(lakana --output json audit list --event-type ACTIVITY_VIEWED --all)
python3 -c 'import json,sys; e=json.loads(sys.argv[1]); assert all(x["event_type"] == "ACTIVITY_VIEWED" for x in e), e' "$out"

# The tutorial ends with how to start over: stop the instance, then reset it. The runner's
# instance is still running here, so the command must refuse and delete nothing.
capture_start
set +e
# docs:start reset
lakana server reset --local
# docs:end reset
status=$?
set -e
capture_end
test "$status" -ne 0
grep -Fq 'Lakana is running from' "$captured"
grep -Fq 'Ctrl-C' "$captured"
test -n "$(lakana --output json sql 'SELECT 1 AS one')"
lakana sql "SELECT * FROM gold.station_ranking" >/dev/null

# The tutorial's last step: stop the instance from another terminal. It must say so and leave.
capture_start
# docs:start stop
lakana server stop
# docs:end stop
capture_end
grep -Fq 'Stopped the local instance' "$captured"
