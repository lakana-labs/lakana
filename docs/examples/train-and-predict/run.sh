#!/usr/bin/env bash
set -euo pipefail
# The commands of the tutorial "Train a model and score with it", in order. The page shows the
# regions; the lines between regions are the checks. Run by scripts/run_docs_examples.sh, which
# starts the local instance (start.sh) and sets EXAMPLE_DIR and WORK.
#
# A Python asset's packages are installed by the worker with uv. The page tells a reader to take
# lakana from the public package index. This runner runs the checkout, whose version is not on
# the index, and a deploy refuses a pin that differs from the instance's version. So the runner
# alone gives the project the wheel of the checkout in a folder `wheelhouse` and a uv index that
# only lakana may use; everything else comes from the public index, so the example needs a
# network connection. The runner is given a folder that holds the wheel:
#   uv build --package lakana --wheel --out-dir DIR && export LAKANA_TEST_WHEELHOUSE=DIR
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"

# The page quotes the output of most commands. A region is run as it is shown; its output is
# written to a file between capture_start and capture_end, printed again afterwards, and checked
# for the lines that do not change from one machine to the next (identifiers, times and sizes do).
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

capture_start
# docs:start init
cd ~
lakana init forecasts --no-sample
cd forecasts
# docs:end init
capture_end
grep -Eq '^Created a Lakana project in .*forecasts\.$' "$captured"
test -f lakana.yaml
test ! -e assets
test ! -e checks

# The tutorial shows each of these files by its own path; here they are copied in.
cp -R "$EXAMPLE_DIR/project/." .

# The page tells the reader to edit the project's pyproject.toml. Here the file is the project's
# own, with the one value the page tells a reader to keep as `lakana init` wrote it (the version
# of lakana, taken from the name of the wheel the runner was given), and with the wheel's folder
# added as the only source of lakana (the deviation this runner needs, see the top).
mkdir wheelhouse
cp "$LAKANA_TEST_WHEELHOUSE"/lakana-[0-9]*.whl wheelhouse/
test "$(find wheelhouse -type f | wc -l)" -eq 1
python3 - <<'PY'
import glob
version = glob.glob("wheelhouse/lakana-[0-9]*.whl")[0].split("/")[-1].split("-")[1]
text = open("pyproject.toml").read()
assert "==<version>" in text
text = text.replace("==<version>", "==" + version)
text += """
[[tool.uv.index]]
name = "lakana"
url = "wheelhouse"
format = "flat"
explicit = true

[tool.uv.sources]
lakana = { index = "lakana" }
"""
open("pyproject.toml", "w").write(text)
PY

# `uv` uses the Python that runs this script: the runner's private HOME hides the Pythons that uv
# downloaded. The lock and the worker's first build ask the public package index.
export UV_NO_PROGRESS=1 UV_PYTHON="$(command -v python3)"
capture_start
# docs:start lock
uv lock
# docs:end lock
capture_end
grep -Eq '^Resolved [0-9]+ packages in ' "$captured"
test -f uv.lock

# docs:start git-identity
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# docs:end git-identity
test "$(git config --global user.email)" = "you@example.com"

capture_start
# docs:start commit
git init
git add -A
git commit -m "A model and its predictions"
# docs:end commit
capture_end

capture_start
# docs:start deploy
lakana repo add forecasts --kind local --url "$PWD"
lakana deploy --env dev
# docs:end deploy
capture_end
grep -q '^Registered repository forecasts' "$captured"
grep -Eq '^Deployed forecasts in dev at [0-9a-f]{40}$' "$captured"
grep -Eq '^│ added +│ bronze\.visits +│$' "$captured"
grep -Eq '^│ added +│ models\.spend_model +│$' "$captured"
grep -Eq '^│ added +│ gold\.spend_predictions +│$' "$captured"

capture_start
# docs:start run
lakana run --env dev
# docs:end run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"
grep -Eq '^│ bronze\.visits +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 4 +│ +│$' "$captured"
grep -Eq '^│ models\.spend_model +│ succeeded +│ 1/3 +│ [0-9.]+s +│ +│ +│$' "$captured"
grep -Eq '^│ gold\.spend_predictions +│ succeeded +│ 1/3 +│ [0-9.]+s +│ 4 +│ +│$' "$captured"

capture_start
# docs:start query
lakana sql "SELECT customer_id, predicted_spend FROM gold.spend_predictions ORDER BY customer_id"
# docs:end query
capture_end
out=$(lakana --output json sql "SELECT customer_id, predicted_spend FROM gold.spend_predictions ORDER BY customer_id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["customer_id"], round(r["predicted_spend"], 6)) for r in rows] == [(1, 12.0), (2, 22.0), (3, 32.0), (4, 42.0)], rows' "$out"

export COLUMNS=120
capture_start
# docs:start artifact-list
lakana artifact list --env dev
# docs:end artifact-list
capture_end
out=$(lakana --output json artifact list --env dev)
python3 -c 'import json,sys; a=json.loads(sys.argv[1]); assert [(x["name"], x["namespace"], x["current_version"]) for x in a] == [("spend_model", "models", 1)], a' "$out"

capture_start
# docs:start artifact-download
lakana artifact download models.spend_model ~/spend-model --env dev
ls ~/spend-model
# docs:end artifact-download
capture_end
grep -Eq '^Downloaded dev/models\.spend_model version 1 to .*/spend-model/model\.joblib$' "$captured"
grep -Fxq 'model.joblib' "$captured"
test "$(stat -c %a ~/spend-model/model.joblib)" = 600

capture_start
# docs:start second-run
lakana run --env dev
# docs:end second-run
capture_end
grep -Eq '^Run [0-9a-f-]{36} succeeded$' "$captured"

capture_start
# docs:start artifact-versions
lakana artifact versions list models.spend_model --env dev
# docs:end artifact-versions
capture_end
# Written to a file there is no terminal to widen, so the line below the table says only how to
# see what was left out. A time and a run identifier are whole, one row per version.
grep -Eq '^│ 2 +│ 20[0-9-]+T[0-9:.]+(Z|\+00:00) +│ [0-9.]+ (B|KiB) +│ [0-9a-f]{12} +│ [0-9a-f-]{36} +│ +│$' "$captured"
grep -Eq '^│ 1 +│ 20[0-9-]+T[0-9:.]+(Z|\+00:00) +│ [0-9.]+ (B|KiB) +│ [0-9a-f]{12} +│ [0-9a-f-]{36} +│ +│$' "$captured"
grep -Fxq 'Not shown: file_count. Use --output json to see it.' "$captured"
out=$(lakana --output json artifact versions list models.spend_model --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert [v["version"] for v in d["versions"]] == [2, 1], d; assert all(v["deleted_at"] is None and v["run_id"] for v in d["versions"]), d' "$out"
# The graph's edges name tables by id: the check maps them to names and asserts the direction
# of the edges, not only that the nodes are there.
LINEAGE_CHECK='
import json, sys
d = json.loads(sys.argv[1])
names = {n["id"]: (n["name"], n["kind"]) for n in d["nodes"]}
edges = {(names[e["source_id"]][0], names[e["target_id"]][0]) for e in d["edges"]}
assert names[d["root_id"]] == ("gold.spend_predictions", "TABLE"), d
assert ("models.spend_model", "ARTIFACT") in names.values(), d
assert ("models.spend_model", "gold.spend_predictions") in edges, edges
assert ("gold.spend_predictions", "models.spend_model") not in edges, edges
assert ("bronze.visits", "models.spend_model") in edges, edges
'
capture_start
# docs:start lineage
lakana table lineage gold.spend_predictions --env dev
# docs:end lineage
capture_end
out=$(lakana --output json table lineage gold.spend_predictions --env dev)
python3 -c "$LINEAGE_CHECK" "$out"

# keep_versions=2: a third run stores version 3 and removes the files of version 1 (its record stays).
lakana run --env dev >/dev/null
out=$(lakana --output json artifact versions list models.spend_model --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); v={x["version"]: x for x in d["versions"]}; assert sorted(v) == [1, 2, 3], d; assert v[1]["deleted_at"] and not v[2]["deleted_at"] and not v[3]["deleted_at"], d' "$out"

# The reference and the delete page show store, delete and drop on a throwaway artifact, so that
# the model of the tutorial is still there at the end. Two more versions are stored first, so that
# the commands that name "version 3" have one.
mkdir ~/scratch
cd ~/scratch
mkdir model-output
echo '{"slope": 1.0, "intercept": 0.0}' > model-output/model.json
capture_start
# docs:start upload
lakana artifact upload models.scratch_model ./model-output --env dev
# docs:end upload
capture_end
grep -Eq '^Stored dev/models\.scratch_model version 1 \(1 file, [0-9]+ B\)$' "$captured"
grep -Fxq 'Sent 1 file; none were already stored.' "$captured"
echo '{"slope": 2.0, "intercept": 0.0}' > model-output/model.json
lakana artifact upload models.scratch_model ./model-output --env dev >/dev/null
echo '{"slope": 3.0, "intercept": 0.0}' > model-output/model.json
lakana artifact upload models.scratch_model ./model-output --env dev >/dev/null
out=$(lakana --output json artifact versions list models.scratch_model --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert [v["version"] for v in d["versions"]] == [3, 2, 1], d' "$out"

# Without a terminal and without --yes the command refuses; its message holds the question it
# would have asked. Nothing is deleted.
status=0
capture_start
lakana artifact versions delete models.scratch_model 3 --env dev </dev/null || status=$?
capture_end
test "$status" -ne 0
grep -Fq "Delete version 3 of dev/models.scratch_model (" "$captured"
# The message is wrapped to the width of the window: compare it as one line.
tr -s '\n ' '  ' < "$captured" | grep -Fq "Refusing without a terminal; pass --yes to confirm."
out=$(lakana --output json artifact versions list models.scratch_model --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert all(v["deleted_at"] is None for v in d["versions"]), d' "$out"

capture_start
# docs:start delete-version
lakana artifact versions delete models.scratch_model 3 --env dev --yes
# docs:end delete-version
capture_end
grep -Eq '^Deleted version 3 of dev/models\.scratch_model\. Freed 1 file \([0-9]+ B\)\.$' "$captured"
out=$(lakana --output json artifact list --env dev --namespace models)
python3 -c 'import json,sys; a={x["name"]: x for x in json.loads(sys.argv[1])}; assert a["scratch_model"]["current_version"] == 2, a; assert a["spend_model"]["current_version"] == 3, a' "$out"

status=0
capture_start
lakana artifact drop models.scratch_model --env dev </dev/null || status=$?
capture_end
test "$status" -ne 0
grep -Fq "Drop artifact dev/models.scratch_model and delete its" "$captured"

capture_start
# docs:start drop
lakana artifact drop models.scratch_model --env dev --yes
# docs:end drop
capture_end
grep -Fq "Dropped artifact dev/models.scratch_model;" "$captured"
out=$(lakana --output json artifact list --env dev)
python3 -c 'import json,sys; assert [x["name"] for x in json.loads(sys.argv[1])] == ["spend_model"], sys.argv[1]' "$out"

# Two versions with the same file: deleting the first frees nothing, because the second still has it.
mkdir twin-output
echo '{"slope": 4.0, "intercept": 0.0}' > twin-output/model.json
lakana artifact upload models.scratch_twin ./twin-output --env dev >/dev/null
lakana artifact upload models.scratch_twin ./twin-output --env dev >/dev/null
capture_start
# docs:start delete-shared
lakana artifact versions delete models.scratch_twin 1 --env dev --yes
# docs:end delete-shared
capture_end
grep -Fxq "Deleted version 1 of dev/models.scratch_twin. Freed no files." "$captured"
grep -Eq '^Kept 1 file \([0-9]+ B\) that version 2 also uses\.$' "$captured"
out=$(lakana --output json artifact versions delete models.scratch_twin 2 --env dev --yes)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert (d["freed_files"], d["kept_files"], d["kept"]) == (1, 0, []), d' "$out"
lakana artifact drop models.scratch_twin --env dev --yes >/dev/null
out=$(lakana --output json artifact list --env dev)
python3 -c 'import json,sys; assert [x["name"] for x in json.loads(sys.argv[1])] == ["spend_model"], sys.argv[1]' "$out"

# An upload that a killed process leaves behind: begun, and never committed or withdrawn. The
# upload command withdraws its own on failure, so the example opens one through the SDK and stops.
sdk_python=$(head -1 "$(command -v lakana)" | sed 's/^#!//')
"$sdk_python" - <<'PY'
import hashlib

import lakana

data = b'{"slope": 9.0, "intercept": 0.0}'
manifest = {
    "format": 1,
    "is_directory": False,
    "files": [{"path": "model.json", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
}
with lakana.Client() as client:
    client.artifacts._begin({"env": "dev", "path": "models", "name": "abandoned_model"}, None, manifest)
PY
capture_start
# docs:start uploads-list
lakana artifact uploads list --env dev
# docs:end uploads-list
capture_end
grep -Eq '^│ [0-9a-f-]{36} +│ models\.abandoned_model +│ ' "$captured"
grep -Fq 'Withdraw one with `lakana artifact uploads abort NAME UPLOAD_ID --env dev`.' "$captured"
out=$(lakana --output json artifact uploads list models.abandoned_model --env dev)
UPLOAD_ID=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert len(d["items"]) == 1 and d["next_cursor"] is None, d; print(d["items"][0]["upload_id"])' "$out")
out=$(lakana --output json artifact uploads list models.spend_model --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1])["items"] == [], sys.argv[1]' "$out"

capture_start
# docs:start uploads-abort
lakana artifact uploads abort models.abandoned_model "$UPLOAD_ID" --env dev
# docs:end uploads-abort
capture_end
grep -Fq "Withdrew upload $UPLOAD_ID of dev/models.abandoned_model" "$captured"

capture_start
# docs:start uploads-list-empty
lakana artifact uploads list --env dev
# docs:end uploads-list-empty
capture_end
grep -Fxq "You have no open uploads in dev." "$captured"
lakana artifact drop models.abandoned_model --env dev --yes >/dev/null
out=$(lakana --output json artifact list --env dev)
python3 -c 'import json,sys; assert [x["name"] for x in json.loads(sys.argv[1])] == ["spend_model"], sys.argv[1]' "$out"

# A schedule builds the table. The commit that declares the job also adds a fifth customer, and
# no manual run follows it, so the five rows below can only come from the scheduled run. The job
# runs every minute, but only once a run principal is bound (see the how-to "Run a job on a
# schedule"): until then it does nothing, so the checks above saw no run of it.
cd ~/forecasts
# docs:start schedule-customer
sed -i 's/(4, 8, 42.0)/(4, 8, 42.0), (5, 10, 52.0)/' assets/bronze/visits.sql
# docs:end schedule-customer
cat "$EXAMPLE_DIR/jobs.yaml" >> lakana.yaml
capture_start
# docs:start schedule-deploy
git add -A
git commit -m "A fifth customer, and a job that refreshes the forecasts every minute"
lakana deploy --env dev
# docs:end schedule-deploy
capture_end
grep -Eq '^Deployed forecasts in dev at [0-9a-f]{40}$' "$captured"
out=$(lakana --output json runs list --env dev --limit 50)
before=$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$out")

capture_start
# docs:start schedule-bind
lakana principal create etl-dev --type service-account
lakana grant create --principal etl-dev --role DATA_ENGINEER --kind ENVIRONMENT --env dev
lakana deployments set-run-as forecasts --env dev --principal etl-dev
# docs:end schedule-bind
capture_end

# Wait for the schedule: poll the list of runs until a run with trigger "schedule" exists. The
# next minute starts within 60 s. Pause the job at once, so that a slow first run (a cold machine)
# cannot be followed by a second fire of the next minute, which the pause would not cancel.
# Then wait for that run to end. A failing command in a poll is reported, not silent.
poll_runs() {
  lakana --output json runs list --env dev --limit 50 || { echo "lakana runs list failed" >&2; return 1; }
}
scheduled=""
deadline=$((SECONDS + 150))
while [ "$SECONDS" -lt "$deadline" ]; do
  out=$(poll_runs) || exit 1
  scheduled=$(python3 -c '
import json, sys
found = [r for r in json.loads(sys.argv[1]) if r.get("trigger") == "schedule"]
print(found[0]["id"] if found else "")
' "$out") || { echo "could not read the list of runs" >&2; exit 1; }
  [ -z "$scheduled" ] || break
  sleep 3
done
test -n "$scheduled" || { echo "no scheduled run started within 150 s" >&2; exit 1; }
capture_start
# docs:start schedule-pause
lakana job pause refresh_forecasts --env dev
# docs:end schedule-pause
capture_end
status=""
while [ "$SECONDS" -lt "$deadline" ]; do
  status=$(lakana --output json runs show "$scheduled" --env dev | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])') \
    || { echo "lakana runs show failed" >&2; exit 1; }
  case "$status" in succeeded|failed|cancelled) break ;; esac
  sleep 3
done
case "$status" in succeeded|failed|cancelled) ;; *) echo "the scheduled run is still $status after the deadline" >&2; exit 1 ;; esac

capture_start
# docs:start schedule-run
lakana --output json runs list --env dev --limit 1
# docs:end schedule-run
capture_end
grep -Fq '"job": "refresh_forecasts"' "$captured"
grep -Fq '"trigger": "schedule"' "$captured"
grep -Fq '"status": "succeeded"' "$captured"
out=$(lakana --output json runs show "$scheduled" --env dev)
python3 -c '
import json, sys
r = json.loads(sys.argv[1])
assert (r["status"], r["trigger"], r["job"], r["run_as"]) == ("succeeded", "schedule", "refresh_forecasts", "etl-dev"), r
assert r["scheduled_for"] and r["triggered_by"] == "etl-dev", r
tasks = {t["asset"]: (t["status"], t["rows_written"]) for t in r["tasks"]}
assert tasks == {"bronze.visits": ("succeeded", 5), "models.spend_model": ("succeeded", None), "gold.spend_predictions": ("succeeded", 5)}, tasks
' "$out"

# The predictions are those of five customers, which only the scheduled run could build. The
# model is exact to rounding: scikit-learn's least squares on points that lie on one line.
capture_start
# docs:start schedule-query
lakana sql "SELECT customer_id, predicted_spend FROM gold.spend_predictions ORDER BY customer_id"
# docs:end schedule-query
capture_end
out=$(lakana --output json sql "SELECT customer_id, predicted_spend FROM gold.spend_predictions ORDER BY customer_id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["customer_id"], round(r["predicted_spend"], 6)) for r in rows] == [(1, 12.0), (2, 22.0), (3, 32.0), (4, 42.0), (5, 52.0)], rows' "$out"

# The newest version of the model was stored by the scheduled run, and no other run has run since:
# versions 1 to 3 came from the three manual runs, so the scheduled run's is 4.
out=$(lakana --output json artifact versions list models.spend_model --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); v={x["version"]: x["run_id"] for x in d["versions"]}; assert max(v) == 4 and v[4] == sys.argv[2], (v, sys.argv[2])' "$out" "$scheduled"
out=$(lakana --output json runs list --env dev --limit 50)
python3 -c '
import json, sys
runs = json.loads(sys.argv[1])
assert len(runs) == int(sys.argv[2]) + 1, runs
assert [r["id"] for r in runs if r.get("trigger") == "schedule"] == [sys.argv[3]], runs
' "$out" "$before" "$scheduled"

# Lineage after the scheduled run: the edges still run from the inputs to the predictions.
out=$(lakana --output json table lineage gold.spend_predictions --env dev)
python3 -c "$LINEAGE_CHECK" "$out"
