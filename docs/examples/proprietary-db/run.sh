#!/usr/bin/env bash
set -euo pipefail
# The how-to "Read from a SQL Server database": the project with the driver, the tcp connection
# from a secret, its test, and a deploy of the two assets. This job has no Docker and so no
# database: the assets read Microsoft's SQL Server image in
# packages/lakana-worker/tests/test_docker_proprietary_db_integration.py (the integration (docker)
# job), which runs these very files. The page shows the regions; the lines between regions are the
# checks. Run by scripts/run_docs_examples.sh, which starts the local instance and sets EXAMPLE_DIR
# and WORK. The project gets lakana as in ingest-files/run.sh (see its top).
: "${LAKANA_TEST_WHEELHOUSE:?set LAKANA_TEST_WHEELHOUSE to a folder that holds the lakana wheel (uv build --package lakana --wheel --out-dir DIR)}"
cd "$WORK"
export COLUMNS=120

# The login's password, as a team keeps it outside the repository (here generated for each run).
ERP_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export ERP_DB_PASSWORD

# docs:start connection
python3 -c 'import json, os; print(json.dumps({"user": "lakana_reader", "password": os.environ["ERP_DB_PASSWORD"]}))' \
  | lakana connection create erp-db --env dev --kind tcp --host erp-db.example.internal --port 1433 --values-stdin
# docs:end connection
out=$(lakana --output json connection show erp-db --env dev)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert (c["kind"], c["address"]) == ("tcp", {"host": "erp-db.example.internal", "port": 1433}), c' "$out"
if lakana --output json connection show erp-db --env dev | grep -Fq "$ERP_DB_PASSWORD"; then
  echo "the connection shows the password" >&2
  exit 1
fi

# The host does not exist (.internal is never a public name), so the test says `dns` and exits 1:
# the region shows the command as a reader types it, the lines around it test the status.
trap - ERR
set +e
# docs:start test
lakana connection test erp-db --env dev
# docs:end test
status=$?
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -eq 1

# The project of the page: `lakana init`'s SQL template with its asset and pyproject.toml replaced
# by the page's (lakana[assets], python-tds and pyOpenSSL), locked and deployed.
cd ~
lakana init sources --template ingest-sql >/dev/null
cd sources
rm assets/bronze/orders.py
cp "$EXAMPLE_DIR"/project/assets/bronze/*.py assets/bronze/
cp "$EXAMPLE_DIR/project/pyproject.toml" pyproject.toml
mkdir wheelhouse
cp "$LAKANA_TEST_WHEELHOUSE"/lakana-[0-9]*.whl wheelhouse/
test "$(find wheelhouse -type f | wc -l)" -eq 1
python3 "$EXAMPLE_DIR/../ingest-files/use_wheelhouse.py" pyproject.toml wheelhouse
export UV_NO_PROGRESS=1 UV_PYTHON="$(command -v python3)"
uv lock >/dev/null
grep -A1 -Fx 'name = "python-tds"' uv.lock | grep -Fqx 'version = "1.17.1"'
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init --quiet
git add -A
git commit --quiet -m "Read the ERP's orders and customers"
lakana repo add sources --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null

# The local instance has no trust bundle (LAKANA_RUN__CA_BUNDLE), so the recipe refuses to send
# the password unencrypted, before it dials anything.
if lakana run --env dev --select bronze.erp_customers >/dev/null 2>&1; then
  echo "the asset ran without a CA" >&2
  exit 1
fi
out=$(lakana --output json runs list --env dev --limit 1 2>/dev/null)
run_id=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0]["id"])' "$out")
lakana runs logs "$run_id" --env dev | grep -Fq "erp-db: no CA to check the server's certificate"
