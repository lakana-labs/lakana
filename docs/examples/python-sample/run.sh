#!/usr/bin/env bash
set -euo pipefail
# The tutorial "Build a table with Python": `lakana init --template python` writes the sample, a
# Python asset and uv.lock; the project is deployed, run, and the Python asset's table queried.
# The page shows the regions; the lines between regions are the checks. Run by
# scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and WORK.
#
# `lakana init` locks the project from the public package index (the example needs a network
# connection). The page's reader takes lakana from there, at the version that ran `lakana init`.
# The examples run the checkout, whose version no index has, so before `lakana init` the wheel of
# the checkout goes into the folder `wheelhouse` of the project and UV_FIND_LINKS names it: uv
# then takes lakana from that folder, the lock names it as `wheelhouse`, and the worker builds
# from it (the way `lakana init` tells a development build to). Every other package comes from the
# public index. The runner is given a folder with the wheel:
#   uv build --package lakana --wheel --out-dir DIR && export LAKANA_TEST_WHEELHOUSE=DIR
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"
export COLUMNS=120

# docs:start git-identity
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# docs:end git-identity

# The wheel must be the version of the CLI that runs `lakana init`, which pins it.
wheels=("$LAKANA_TEST_WHEELHOUSE"/lakana-[0-9]*.whl)
test "${#wheels[@]}" -eq 1
wheel_version=$(basename "${wheels[0]}" | cut -d- -f2)
cli_version=$(python3 -c 'import lakana; print(lakana.__version__)')
test "$wheel_version" = "$cli_version" || {
  echo "the wheel in LAKANA_TEST_WHEELHOUSE is lakana $wheel_version, the CLI is $cli_version: build the wheel from the same commit" >&2
  exit 1
}
mkdir -p ~/weather/wheelhouse
cp "${wheels[0]}" ~/weather/wheelhouse/
export UV_FIND_LINKS=wheelhouse UV_NO_PROGRESS=1 UV_PYTHON="$(command -v python3)"

# docs:start init
cd ~
lakana init weather --template python
cd weather
# docs:end init
# The asset the page shows is the one `lakana init` wrote, and the lock takes lakana from the
# folder and nothing from a direct URL, a git repository or a local path.
diff "$EXAMPLE_DIR/project/assets/gold/temperature_anomalies.py" assets/gold/temperature_anomalies.py
test -f uv.lock
grep -Fq 'source = { registry = "wheelhouse" }' uv.lock
grep -Fq 'lakana sql "SELECT * FROM gold.temperature_anomalies' README.md
unset UV_FIND_LINKS

# docs:start commit
git init --quiet
git add -A
git commit --quiet -m "Initial project"
# docs:end commit
git ls-files --error-unmatch uv.lock assets/gold/temperature_anomalies.py >/dev/null

# docs:start deploy
lakana repo add weather --kind local --url "$PWD"
lakana deploy --env dev
# docs:end deploy

# docs:start run
lakana run --env dev
# docs:end run
out=$(lakana --output json runs list --env dev --limit 1)
python3 -c '
import json, sys
(run,) = json.loads(sys.argv[1])
assert run["status"] == "succeeded", run
' "$out"
out=$(lakana --output json sql "SELECT count(*) AS n FROM gold.temperature_anomalies")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 18}], sys.argv[1]' "$out"

# docs:start query
lakana sql "SELECT * FROM gold.temperature_anomalies WHERE station_id = 'B2' ORDER BY day"
# docs:end query
out=$(lakana --output json sql "SELECT station_id, min(anomaly_c) AS lo, max(anomaly_c) AS hi FROM gold.temperature_anomalies GROUP BY station_id ORDER BY station_id")
python3 -c '
import json, sys
rows = json.loads(sys.argv[1])
assert [(r["station_id"], r["lo"], r["hi"]) for r in rows] == [("A1", -1.0, 1.0), ("B2", -1.0, 1.0), ("C3", -1.0, 1.0)], rows
' "$out"
