#!/usr/bin/env bash
set -euo pipefail
# The generic recipe of the how-to "Load data from a source": an asset that reads a declared
# connection with `connections.get` and its own HTTPS client (httpx), for a source no helper reads.
# The API is the stand-in of ingest-rest (its stand_in.py and make_certs.py; see ingest-rest/run.sh).
# The page shows the regions; the lines between regions are the checks. Run by
# scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR and WORK. The
# project gets lakana as in ingest-files/run.sh (see its top).
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"
export COLUMNS=120
rest="$EXAMPLE_DIR/../ingest-rest"

python3 "$rest/make_certs.py" ~/api
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/api/token.txt
cp "$rest/items-1.json" ~/api/items.json
python3 "$rest/stand_in.py" ~/api &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  [ -s ~/api/port ] && break
  kill -0 "$api_pid" || { echo "the example's API stopped before it listened" >&2; exit 1; }
  sleep 0.2
done
API_PORT=$(cat ~/api/port)
lakana connection create source-api --env dev --kind http --url "https://localhost:$API_PORT/api" \
  --auth bearer --ca-file ~/api/ca.pem --password-stdin < ~/api/token.txt >/dev/null

# The project of the REST template, with the template's asset replaced by the one the page shows.
cd ~
lakana init sources --template ingest-rest >/dev/null
cd sources
cp "$EXAMPLE_DIR/project/assets/bronze/items.py" assets/bronze/items.py
mkdir wheelhouse
cp "$LAKANA_TEST_WHEELHOUSE"/lakana-[0-9]*.whl wheelhouse/
test "$(find wheelhouse -type f | wc -l)" -eq 1
python3 "$EXAMPLE_DIR/../ingest-files/use_wheelhouse.py" pyproject.toml wheelhouse
export UV_NO_PROGRESS=1 UV_PYTHON="$(command -v python3)"
uv lock >/dev/null
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init --quiet
git add -A
git commit --quiet -m "Read the items with httpx"
lakana repo add sources --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null

lakana run --env dev >/dev/null
# docs:start query
lakana sql "SELECT id, name, price, updated_at FROM bronze.items ORDER BY id"
# docs:end query
out=$(lakana --output json sql "SELECT id, name FROM bronze.items ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["name"]) for r in rows] == [(1, "Anchor"), (2, "Buoy"), (3, "Cleat")], rows' "$out"

# Without a write mode the asset replaces its table: the next run holds what the API holds then.
cp "$rest/items-2.json" ~/api/items.json
lakana run --env dev >/dev/null
out=$(lakana --output json sql "SELECT id, name FROM bronze.items ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["name"]) for r in rows] == [(1, "Anchor"), (2, "Buoy, large"), (3, "Cleat"), (4, "Deck mop")], rows' "$out"
# The token stays out of the task's log.
out=$(lakana --output json runs list --env dev --limit 1 2>/dev/null)
run_id=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0]["id"])' "$out")
if lakana runs logs "$run_id" --env dev | grep -Fq "$(cat ~/api/token.txt)"; then
  echo "the task's log holds the token" >&2
  exit 1
fi
