#!/usr/bin/env bash
set -euo pipefail
# The SQL recipe of the how-to "Load data from a source": `lakana init --template ingest-sql`, read
# from a SQLite file through a `sqlite-file` connection below an ingestion root (start.sh; the
# template's README names PostgreSQL, which needs a server), two runs, and the table's lineage. The
# page shows the regions; the lines between regions are the checks. Run by
# scripts/run_docs_examples.sh, which starts the local instance (start.sh) and sets EXAMPLE_DIR and
# WORK. The project gets lakana as in ingest-files/run.sh (see its top).
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"
export COLUMNS=120

# docs:start init
cd ~
lakana init sources --template ingest-sql
cd sources
# docs:end init
diff -r "$EXAMPLE_DIR/project/assets" assets
grep -Fq "lakana connection create source-db --env dev --kind postgresql" README.md

# The source database: a SQLite file with three orders, made with Python's own sqlite3.
mkdir -p ~/landing
python3 "$EXAMPLE_DIR/orders_db.py" ~/landing/shop.db 1 2 3

# docs:start connection
lakana connection create source-db --env dev --kind sqlite-file --root landing --path shop.db
# docs:end connection
out=$(lakana --output json connection show source-db --env dev)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert (c["kind"], c["address"]) == ("sqlite-file", {"root": "landing", "path": "shop.db"}), c' "$out"

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
git commit --quiet -m "Read the orders"
lakana repo add sources --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null

lakana run --env dev >/dev/null
out=$(lakana --output json sql "SELECT id FROM bronze.orders ORDER BY id")
python3 -c 'import json,sys; assert [r["id"] for r in json.loads(sys.argv[1])] == [1, 2, 3], sys.argv[1]' "$out"

# Two new orders; the next run appends only those, the ids after the largest one loaded.
python3 "$EXAMPLE_DIR/orders_db.py" ~/landing/shop.db 4 5
lakana run --env dev >/dev/null

# docs:start query
lakana sql "SELECT id, customer, amount FROM bronze.orders ORDER BY id"
# docs:end query
out=$(lakana --output json sql "SELECT id, count(*) AS n FROM bronze.orders GROUP BY id ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["n"]) for r in rows] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)], rows' "$out"

# docs:start lineage
lakana table lineage bronze.orders --env dev
# docs:end lineage
out=$(lakana --output json table lineage bronze.orders --env dev)
python3 -c '
import json, sys
d = json.loads(sys.argv[1])
sources = [(n["kind"], n["name"], n.get("connection_kind")) for n in d["nodes"] if n["kind"] == "CONNECTION"]
assert sources == [("CONNECTION", "source-db", "sqlite-file")], d
' "$out"
