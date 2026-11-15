#!/usr/bin/env bash
# The offline install, proven (#493): the container image runs with no network at all, the worker
# forbidden to download DuckDB's extension, and still starts local mode, creates a table, appends
# to it and answers `lakana sql`. `docker run --network none` gives the container a loopback
# interface and nothing else (`unshare -rn` is not available on every runner).
# Usage: offline_install_test.sh IMAGE   (the image `docker build` made; building needs the network,
# running does not). Documented in docs/site/how-to/install-without-internet-access.md.
set -euo pipefail
IMAGE=${1:?usage: offline_install_test.sh IMAGE}

# Runs inside the container, as the image's own user.
inner='
set -euo pipefail
export NO_COLOR=1
log=$(mktemp)
lakana server start --local > "$log" 2>&1 &
pid=$!
# On any failure, show the server log; then stop the server and keep the exit status.
trap "rc=\$?; [ \$rc -eq 0 ] || cat $log; kill -INT $pid 2>/dev/null || true; wait $pid 2>/dev/null || true; exit \$rc" EXIT
for _ in $(seq 1 120); do
  grep -q "Lakana is running" "$log" && break
  kill -0 "$pid" 2>/dev/null || exit 1
  sleep 0.5
done
grep -q "Lakana is running" "$log" || exit 1
out=$(lakana --output json sql "SELECT 42 AS answer")
python -c "import json,sys; assert json.loads(sys.argv[1]) == [{\"answer\": 42}], sys.argv[1]" "$out"
lakana table create bronze.demo --column id:long --column "name:string?"
printf "id,name\n1,alpha\n2,beta\n" > /var/tmp/lakana/demo.csv
lakana table append bronze.demo /var/tmp/lakana/demo.csv
out=$(lakana --output json sql "SELECT id, name FROM bronze.demo ORDER BY id")
python -c "import json,sys; assert json.loads(sys.argv[1]) == [{\"id\": 1, \"name\": \"alpha\"}, {\"id\": 2, \"name\": \"beta\"}], sys.argv[1]" "$out"
echo "offline install test: passed"
'

docker run --rm --network none \
  -e LAKANA_WORKER__EXTENSION_SOURCE=none \
  --entrypoint /usr/bin/tini "$IMAGE" -- bash -c "$inner"
