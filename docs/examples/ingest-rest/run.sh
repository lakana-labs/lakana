#!/usr/bin/env bash
set -euo pipefail
# The REST recipe of the how-to "Load data from a source": `lakana init --template ingest-rest`,
# an `http` connection with a bearer token and its own CA, and three runs that merge what changed.
# The API is a stand-in this script starts on this machine (stand_in.py, HTTPS with a CA that
# make_certs.py makes for each run; the token is made for each run too). Under the local runner a
# task reaches 127.0.0.1 directly. The page shows the regions; the lines between regions are the
# checks. Run by scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR
# and WORK. The project gets lakana as in ingest-files/run.sh (see its top).
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"
export COLUMNS=120

# The API: a CA and a certificate, a token nobody else has, three items.
python3 "$EXAMPLE_DIR/make_certs.py" ~/api
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/api/token.txt
cp "$EXAMPLE_DIR/items-1.json" ~/api/items.json
python3 "$EXAMPLE_DIR/stand_in.py" ~/api &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  [ -s ~/api/port ] && break
  kill -0 "$api_pid" || { echo "the example's API stopped before it listened" >&2; exit 1; }
  sleep 0.2
done
API_PORT=$(cat ~/api/port)
cp ~/api/ca.pem ~/api/token.txt "$WORK"/

# docs:start init
cd ~
lakana init sources --template ingest-rest
cd sources
# docs:end init
diff -r "$EXAMPLE_DIR/project/assets" assets
grep -Fq "lakana connection create source-api --env dev --kind http" README.md

cd "$WORK"
# docs:start connection
lakana connection create source-api --env dev --kind http --url "https://localhost:$API_PORT/api" \
  --auth bearer --ca-file ca.pem --password-stdin < token.txt
# docs:end connection
out=$(lakana --output json connection show source-api --env dev)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert (c["kind"], c["settings"]["auth"], c["secret"]) == ("http", "bearer", "source-api-connection"), c' "$out"
# The token is in the connection's secret only: no answer shows it.
if grep -Fq "$(cat token.txt)" <<< "$out"; then echo "connection show printed the token" >&2; exit 1; fi

lakana connection test source-api --env dev
cd ~/sources

mkdir wheelhouse
cp "$LAKANA_TEST_WHEELHOUSE"/lakana-[0-9]*.whl wheelhouse/
test "$(find wheelhouse -type f | wc -l)" -eq 1
python3 "$EXAMPLE_DIR/../ingest-files/use_wheelhouse.py" pyproject.toml wheelhouse
export UV_NO_PROGRESS=1 UV_PYTHON="$(command -v python3)"
uv lock >/dev/null
test -f uv.lock

git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init --quiet
git add -A
git commit --quiet -m "Read the items"
lakana repo add sources --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null

# The first run reads every item, two pages; the API's text `updated_at` becomes a timestamp.
lakana run --env dev >/dev/null
out=$(lakana --output json sql "SELECT id, name, epoch(updated_at) AS at FROM bronze.items ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["name"]) for r in rows] == [(1, "Anchor"), (2, "Buoy"), (3, "Cleat")], rows; assert rows[2]["at"] == 1790848800, rows' "$out"

# One item changes and one is added. The next run asks only for what changed after the newest
# `updated_at` loaded and merges it on `id`: the change replaces its row, the new item is added.
cp "$EXAMPLE_DIR/items-2.json" ~/api/items.json
lakana run --env dev >/dev/null

# docs:start query
lakana sql "SELECT id, name, price, updated_at FROM bronze.items ORDER BY id"
# docs:end query
out=$(lakana --output json sql "SELECT id, name, count(*) OVER (PARTITION BY id) AS n FROM bronze.items ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["name"], r["n"]) for r in rows] == [(1, "Anchor", 1), (2, "Buoy, large", 1), (3, "Cleat", 1), (4, "Deck mop", 1)], rows' "$out"

# Nothing changed: the API answers an empty page, the run succeeds and writes nothing.
lakana run --env dev >/dev/null
out=$(lakana --output json runs list --env dev --limit 1 2>/dev/null)
run_id=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0]["id"])' "$out")
out=$(lakana --output json runs show "$run_id" --env dev)
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["status"] == "succeeded", r; assert [(t["asset"], t["status"], t["rows_written"]) for t in r["tasks"]] == [("bronze.items", "succeeded", 0)], r["tasks"]' "$out"
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.items")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 4}], sys.argv[1]' "$out"
