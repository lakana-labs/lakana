#!/usr/bin/env bash
set -euo pipefail
# The files recipe of the how-to "Load data from a source": `lakana init --template ingest-files`,
# a `directory` connection below an ingestion root (start.sh), two runs, and a schedule. The page
# shows the regions; the lines between regions are the checks. Run by scripts/run_docs_examples.sh,
# which starts the local instance (start.sh) and sets EXAMPLE_DIR and WORK.
#
# The asset is a Python asset, so the project gets lakana the way train-and-predict/run.sh does:
# the wheel of the checkout in a folder `wheelhouse`, every other package from the public package
# index (the example needs a network connection). The runner is given a folder with the wheel:
#   uv build --package lakana --wheel --out-dir DIR && export LAKANA_TEST_WHEELHOUSE=DIR
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"
export COLUMNS=120

# docs:start init
cd ~
lakana init sources --template ingest-files
cd sources
# docs:end init
# The asset the page shows is the one `lakana init` wrote.
diff -r "$EXAMPLE_DIR/project/assets" assets
grep -Fq "lakana connection create exports --env dev --kind directory" README.md

# The source system's exports: two files, both written on 1 October.
mkdir -p ~/landing/exports
cp "$EXAMPLE_DIR/exports/orders-1.csv" "$EXAMPLE_DIR/exports/orders-2.csv" ~/landing/exports/
touch -d '2026-10-01 08:00:00 UTC' ~/landing/exports/orders-1.csv ~/landing/exports/orders-2.csv

# docs:start connection
lakana connection create exports --env dev --kind directory --root landing --path exports
# docs:end connection
out=$(lakana --output json connection show exports --env dev)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert (c["kind"], c["address"]) == ("directory", {"root": "landing", "path": "exports"}), c' "$out"

# docs:start connection-test
lakana connection test exports --env dev
# docs:end connection-test

# The project's lakana comes from the wheel of the checkout (see the top and use_wheelhouse.py).
mkdir wheelhouse
cp "$LAKANA_TEST_WHEELHOUSE"/lakana-[0-9]*.whl wheelhouse/
test "$(find wheelhouse -type f | wc -l)" -eq 1
python3 "$EXAMPLE_DIR/use_wheelhouse.py" pyproject.toml wheelhouse
export UV_NO_PROGRESS=1 UV_PYTHON="$(command -v python3)"
# docs:start lock
uv lock
# docs:end lock
test -f uv.lock

git config --global user.name "Your Name"
git config --global user.email "you@example.com"
# docs:start deploy
git init --quiet
git add -A
git commit --quiet -m "Read the exported orders"
lakana repo add sources --kind local --url "$PWD"
lakana deploy --env dev
# docs:end deploy

# docs:start run
lakana run --env dev
# docs:end run
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.exports")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 3}], sys.argv[1]' "$out"

# A third file arrives, written later than the first two (`since` reads files changed strictly
# after the newest one loaded).
cp "$EXAMPLE_DIR/exports/orders-3.csv" ~/landing/exports/
touch -d '2026-10-02 08:00:00 UTC' ~/landing/exports/orders-3.csv

# docs:start run-again
lakana run --env dev
# docs:end run-again

# docs:start query
lakana sql "SELECT order_id, customer, amount, _source_modified FROM bronze.exports ORDER BY order_id"
# docs:end query
out=$(lakana --output json sql "SELECT order_id, count(*) AS n FROM bronze.exports GROUP BY order_id ORDER BY order_id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["order_id"], r["n"]) for r in rows] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)], rows' "$out"

# A run with no new file succeeds and writes nothing.
lakana run --env dev >/dev/null
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.exports")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 5}], sys.argv[1]' "$out"

# The page shows jobs.yaml whole; the job is added to lakana.yaml and deployed.
cat "$EXAMPLE_DIR/jobs.yaml" >> lakana.yaml
# docs:start schedule
git commit --quiet -am "Load the exports every hour"
lakana deploy --env dev
# docs:end schedule
out=$(lakana --output json job list --env dev)
python3 -c 'import json,sys; jobs=json.loads(sys.argv[1]); assert [j["name"] for j in jobs] == ["load_exports"], jobs' "$out"
