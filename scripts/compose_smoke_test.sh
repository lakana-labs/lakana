#!/usr/bin/env bash
# Compose team bundle end to end: init → up → create/append/query →
# project init, commit, register and deploy → run, including one Python
# asset (the launcher builds its environment in a build container and runs it in a task
# container) and one artifact, stored
# by one task and read by the next → restart
# (idempotence) → query again → a worker per
# kind of work, the default worker stopped (the deploy, the canary and container checks and the
# run again, now on `worker-pipeline`) → down -v.
#
# TASK_IMAGE names the task images without -py3.X (default: IMAGE's repository with -task, same
# tag). Those of 3.11 (the launcher's start-up probes) and of the image's Python are pulled
# (a failed pull fails the run), or built from this checkout when the tag is local (CI's
# lakana-task:ci).
#
# Two sequences are the documentation's own commands, sourced from docs/examples (the page shows
# their regions): a second environment with the bundle's storage secret
# (compose-add-environment.sh) and a backup, a change and a restore into a new directory
# (compose-restore.sh).
#
# Last, M6's exit criterion: the three ingestion templates on a schedule against a file directory,
# a PostgreSQL database and a REST API that run outside the stack, what lands in bronze, the
# lineage to the database, the proxy's refusal of a host no connection names, and `lakana load`.
#
# The Python asset's packages come from an offline wheel directory: LAKANA_WHEELHOUSE, or one
# built here by scripts/build_wheelhouse.sh (the only step that asks a package index). Its lakana
# wheel must have the version of IMAGE; both are built from the same checkout.
set -euo pipefail
IMAGE=${1:?usage: compose_smoke_test.sh IMAGE [TASK_IMAGE]}
TASK_IMAGE=${2:-${IMAGE%:*}-task:${IMAGE##*:}}
here=$(cd "$(dirname "$0")" && pwd)
# A unique project name per run: every bundle
# `bundle init` writes uses the fixed Compose project `lakana` (compose.yaml's
# `name:`), and the environment variable takes precedence over that `name:`
# key. Without this, this script's own `down -v` at cleanup would tear down
# and delete the volumes of any real `lakana` bundle already running on the
# same Docker host.
# (Written into the bundle's .env below; a variable already in the shell would override it.)
unset COMPOSE_PROJECT_NAME
project_name="lakana-smoke-$$"
work=$(mktemp -d)
export XDG_CONFIG_HOME="$work/config" NO_COLOR=1
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
bundle="$work/bundle"
# The directory the artifact is downloaded into, as `first` and, after the restart,
# `second`. Under $HOME, not $work: a download is read-only, so a failed assertion must still
# remove it (cleanup below); the SDK stages a download in a hidden sibling of its target
# (`.<name>.lakana-partial-*`), which a killed download leaves behind, so the siblings live in
# this directory too and go with it.
copy="$HOME/.lakana-smoke-artifact-$$"
cleanup() {
  status=$?
  if [ -d "$copy" ]; then chmod -R u+w "$copy" || true; rm -rf "${copy:?}" || true; fi
  if [ $status -ne 0 ]; then (cd "$bundle" && docker compose logs --no-color) || true; fi
  (cd "$bundle" && docker compose down -v --remove-orphans) >/dev/null 2>&1 || true
  # The launcher's environment and cache volumes are not the bundle's own: `down -v` leaves them.
  docker volume ls -q --filter "label=lakana.launcher.stack=$project_name" | xargs -r docker volume rm >/dev/null 2>&1 || true
  # The ingestion sources (the database and the REST API), started by this run outside the bundle.
  if [ $status -ne 0 ]; then
    for source in $(docker ps -aq --filter "label=lakana.smoke.source=$project_name"); do docker logs --tail 40 "$source" || true; done
  fi
  docker ps -aq --filter "label=lakana.smoke.source=$project_name" | xargs -r docker rm -f >/dev/null 2>&1 || true
  rm -rf "$work"
  exit $status
}
trap cleanup EXIT
# The wheels must be for the image's interpreter, so ask the image which one it has.
python=$(docker run --rm "$IMAGE" python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
# The task images. A published one (a reference with a registry or namespace) is pulled, and a
# failed pull ends the run: the release smoke must run on the images it published. Only a local
# tag (CI's lakana-task:ci) is built from this checkout when it is missing.
for version in 3.11 "$python"; do
  reference="$TASK_IMAGE-py$version"
  if docker image inspect "$reference" >/dev/null 2>&1; then continue; fi
  case "$TASK_IMAGE" in
    */*) docker pull "$reference" ;;
    *)
      # The check environment's packages, by hash from uv.lock, as the workflows export them.
      mkdir -p "$here/../dist"
      uv export --frozen --only-group check --no-emit-workspace -o "$here/../dist/check-requirements.txt"
      docker build -f "$here/../Dockerfile.task" --build-arg "PYTHON=$version" -t "$reference" "$here/.." ;;
  esac
done
wheelhouse=${LAKANA_WHEELHOUSE:-}
if [ -z "$wheelhouse" ]; then
  wheelhouse="$work/wheelhouse"
  LAKANA_WHEELHOUSE_PYTHON=$python bash "$here/build_wheelhouse.sh" "$wheelhouse"
fi
wheels=("$wheelhouse"/lakana-[0-9]*.whl)
if [ "${#wheels[@]}" -ne 1 ] || [ ! -e "${wheels[0]}" ]; then
  echo "$wheelhouse must hold exactly one lakana wheel (the image's version), found ${#wheels[@]}: ${wheels[*]}" >&2
  exit 1
fi
image_version=$(docker run --rm "$IMAGE" python -c 'import lakana; print(lakana.__version__)')
wheel_version=$(basename "${wheels[0]}" | cut -d- -f2)
if [ "$image_version" != "$wheel_version" ]; then
  echo "the wheel directory holds lakana $wheel_version, the image $IMAGE has $image_version: build both from the same checkout" >&2
  exit 1
fi
lakana bundle init "$bundle" --image "$IMAGE" --task-image "$TASK_IMAGE" --port "$port"
# The name goes in the bundle's .env, as the guide tells an operator to do, not in the shell:
# the documented backup and restore commands must find the volumes through Compose alone.
echo "COMPOSE_PROJECT_NAME=$project_name" >> "$bundle/.env"
cd "$bundle"
# `worker.depends_on.setup: {condition: service_completed_successfully}` in
# the compose template is what
# makes `--wait` block on `setup` and fail if it exits non-zero -- the same
# mechanism `server`/`worker` already use for `migrate`. An earlier version
# of this fix appended a separate `docker compose wait setup` after `up
# --wait` instead; that raced `setup`'s own exit two different ways on a
# restart, where `setup` sends two GETs and exits in under a second: `up
# --wait` could poll `setup` right after it had already exited and report
# `up` itself as failed, or `wait setup` could run after the container was
# already gone and find no container to wait on ("no containers for project
# ..."). Gating through a dependency has neither race -- measured 6 of 6
# clean restarts with it, versus roughly 1 of 3 with the separate command.
docker compose up -d --wait --wait-timeout 180
lakana --profile team table create bronze.demo --column id:long --column name:string?
printf 'id,name\n1,alpha\n2,beta\n' > "$work/demo.csv"
lakana --profile team table append bronze.demo "$work/demo.csv"
check() {
  out=$(lakana --profile team --output json sql "SELECT id, name FROM bronze.demo ORDER BY id")
  python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"name":"alpha"},{"id":2,"name":"beta"}], sys.argv[1]' "$out"
}
check
# The readiness route proves the database, the storage of `dev` and a worker. `up --wait` returned
# once each worker's own healthcheck saw its heartbeat, so it answers 200 within a few tries (an
# answer is reused for 5 s).
ready() {
  python3 -c 'import json,sys,urllib.request; r=urllib.request.urlopen(sys.argv[1], timeout=10); assert json.load(r) == {"status": "ready"}' "http://127.0.0.1:$port/readyz" 2>/dev/null
}
for _ in $(seq 1 15); do ready && break; sleep 2; done
ready || { echo "/readyz did not answer 200: $(curl -s "http://127.0.0.1:$port/readyz")" >&2; exit 1; }
# The documentation's commands use the default profile, not `--profile team`.
export LAKANA_PROFILE=team
# Add an environment to the bundle (how-to "Add an environment to a Compose instance").
# shellcheck source=../docs/examples/compose-add-environment.sh
source "$here/../docs/examples/compose-add-environment.sh"
# Init a project in the mounted repos/ directory, commit, register, deploy to dev.
project="$bundle/repos/demo"
lakana init "$project"
bash "$here/python_asset_project.sh" "$project" "$wheelhouse" "$python" artifact
git -C "$project" init -q
git -C "$project" -c user.name=smoke -c user.email=smoke@lakana.invalid add -A
git -C "$project" -c user.name=smoke -c user.email=smoke@lakana.invalid commit -qm "smoke"
# The worker (uid 10001) trusts this host-owned repository via a per-fetch
# safe.directory, so the source repo's own config is read by upload-pack. Plant
# config that would execute a command if git ever honoured it here, with the
# image's git, and assert after the deploy that none of it ran.
hooks="$project/.canary-hooks"
mkdir -p "$hooks"
for hook in pre-upload post-checkout reference-transaction post-index-change pre-auto-gc; do
  printf '#!/bin/sh\ntouch /tmp/lakana-canary-hook-%s\n' "$hook" > "$hooks/$hook"
  chmod +x "$hooks/$hook"
done
canary() { git -C "$project" config "$1" "$2"; }
canary core.fsmonitor 'touch /tmp/lakana-canary-fsmonitor'
canary core.hooksPath /repos/demo/.canary-hooks
canary uploadpack.packObjectsHook 'touch /tmp/lakana-canary-packhook; false'
canary core.alternateRefsCommand 'touch /tmp/lakana-canary-altrefs'
lakana --profile team repo add demo --kind local --url /repos/demo
out=$(lakana --profile team --output json deploy demo --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["status"]=="succeeded", d' "$out"
ran=$(docker compose exec -T worker sh -c 'ls /tmp/lakana-canary-* 2>/dev/null || true')
if [ -n "$ran" ]; then echo "source repo config executed in the worker: $ran" >&2; exit 1; fi
if ls /tmp/lakana-canary-* >/dev/null 2>&1; then echo "source repo config executed on the host" >&2; exit 1; fi
# Run the deployed project and read the table it built.
lakana --profile team run demo --env dev
out=$(lakana --profile team --output json sql "SELECT id, greeting FROM bronze.example")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"greeting":"hello"}], sys.argv[1]' "$out"
# The same run built the Python asset's environment in a build container and ran the asset in
# a task container; the worker holds no Docker socket.
out=$(lakana --profile team --output json sql "SELECT id, doubled FROM silver.doubled")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"doubled":2}], sys.argv[1]' "$out"
cache=/home/lakana/.cache/lakana-worker
mode=$(docker compose exec -T worker stat -c '%u %a' "$cache")
if [ "$mode" != "10001 700" ]; then echo "worker cache is '$mode', expected '10001 700'" >&2; exit 1; fi
docker compose exec -T worker sh -c 'test ! -e /var/run/docker.sock'
docker volume ls -q --filter "label=lakana.launcher.stack=$project_name" | grep -- '-env-' >/dev/null
# The same run stored version 1 of an artifact and scored with it; the bytes went
# through the REST API into the bundle's object storage.
out=$(lakana --profile team --output json sql "SELECT id, doubled FROM gold.by_model")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"doubled":2}], sys.argv[1]' "$out"
out=$(lakana --profile team --output json artifact list --env dev)
python3 -c 'import json,sys; a=json.loads(sys.argv[1]); assert [(x["name"], x["current_version"]) for x in a] == [("doubler", 1)], a' "$out"
mkdir -p "$copy"
lakana --profile team artifact download models.doubler "$copy/first" --env dev
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["factor"] == 2' "$copy/first/model.json"
# SeaweedFS's internal HTTP APIs are unreachable from the worker (#14): `seaweedfs` is only the
# S3 gateway, and the master, volume server and filer live on `seaweedfs-core`, on a network the
# worker does not join. Run from inside the worker, with the image's own Python (it has no curl).
docker compose exec -T worker python - <<'PY'
import socket
import urllib.error
import urllib.request

# The internal host is not even resolvable (assumes the host's DNS has no wildcard search
# domain, which would resolve any name); the gateway's host refuses every internal port.
try:
    socket.getaddrinfo("seaweedfs-core", 9333)
except socket.gaierror:
    pass
else:
    raise SystemExit("the worker resolves seaweedfs-core: the internal network is reachable")
for port in (9333, 19333, 8080, 18080, 8888, 18888, 8181, 9101):
    try:
        socket.create_connection(("seaweedfs", port), timeout=3).close()
    except OSError:
        continue
    raise SystemExit(f"the worker reached seaweedfs:{port}, an internal SeaweedFS port")
# S3 through the configured endpoint answers; with no credentials it refuses (403).
try:
    urllib.request.urlopen("http://seaweedfs:8333/lakana/", timeout=5)
except urllib.error.HTTPError as err:
    if err.code != 403:
        raise SystemExit(f"S3 without credentials answered {err.code}, expected 403")
else:
    raise SystemExit("S3 answered an unauthenticated bucket listing")
PY
# A restart is a no-op and data survives
docker compose down
docker compose up -d --wait --wait-timeout 180
check
# The bytes of the artifact are in the bundle's object storage volume, so they survive too: the
# same version downloaded again is the same files.
lakana --profile team artifact download models.doubler "$copy/second" --env dev
diff -r "$copy/first" "$copy/second"
# Back up, change, lose the instance and restore it into a new directory (how-to "Deploying with
# Compose", Backups and Restore). From here on the bundle runs in $RESTORED.
RESTORED="$work/restored"
# shellcheck source=../docs/examples/compose-restore.sh
source "$here/../docs/examples/compose-restore.sh"
# The restored storage holds the artifact's bytes: the same version downloaded again is the same files.
lakana --profile team artifact download models.doubler "$copy/third" --env dev
diff -r "$copy/first" "$copy/third"
check
# Workers for one kind of work. The bundle starts none by default; two numbers in .env
# start one of each next to the two `all` workers. Then the default workers go to zero, so that
# what follows can only run on the specialised ones: ad-hoc queries on `worker-interactive`, the
# deploy and the run on `worker-pipeline`. Compose scales a service to and from zero replicas.
set_env() { sed -i "s/^$1=.*/$1='$2'/" .env; grep -q "^$1='$2'" .env; }
alive_takes() {
  out=$(lakana --profile team --output json worker list)
  python3 -c 'import json,sys; print(json.dumps(sorted(w["takes"] for w in json.loads(sys.argv[1]) if w["status"] == "alive")))' "$out"
}
# A worker registers a moment after its container is healthy, and a stopped one removes its row.
wait_for_takes() {
  for _ in $(seq 1 30); do
    got=$(alive_takes)
    if [ "$got" = "$1" ]; then return 0; fi
    sleep 2
  done
  echo "alive workers take $got, expected $1" >&2
  return 1
}
set_env LAKANA_WORKERS_INTERACTIVE 1
set_env LAKANA_WORKERS_PIPELINE 1
docker compose up -d --wait --wait-timeout 180
wait_for_takes '["all", "all", "interactive", "pipeline"]'
check
set_env LAKANA_WORKERS 0
# Without --wait: Compose 2.x fails `up --wait` with "is missing dependency worker" when it removes
# the replicas of a service that goes to zero (2.24 to 2.38 at least; v5 does not). The second
# command waits for the rest.
docker compose up -d
docker compose up -d --wait --wait-timeout 180
wait_for_takes '["interactive", "pipeline"]'
check
out=$(lakana --profile team --output json deploy demo --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["status"]=="succeeded", d' "$out"
ran=$(docker compose exec -T worker-pipeline sh -c 'ls /tmp/lakana-canary-* 2>/dev/null || true')
if [ -n "$ran" ]; then echo "source repo config executed in worker-pipeline: $ran" >&2; exit 1; fi
if ls /tmp/lakana-canary-* >/dev/null 2>&1; then echo "source repo config executed on the host" >&2; exit 1; fi
lakana --profile team run demo --env dev
out=$(lakana --profile team --output json sql "SELECT id, doubled FROM silver.doubled")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"id":1,"doubled":2}], sys.argv[1]' "$out"
mode=$(docker compose exec -T worker-pipeline stat -c '%u %a' "$cache")
if [ "$mode" != "10001 700" ]; then echo "worker-pipeline cache is '$mode', expected '10001 700'" >&2; exit 1; fi
docker compose exec -T worker-pipeline sh -c 'test ! -e /var/run/docker.sock'
docker volume ls -q --filter "label=lakana.launcher.stack=$project_name" | grep -- '-env-' >/dev/null
# M6's exit criterion (#1): scheduled, incremental ingestion lands in bronze from a file directory,
# a SQL database and a REST API, each asset the one `lakana init --template` writes, under the
# Docker runner; a task reaches nothing its asset does not declare; a local file loads with one
# command. From here the bundle runs in $RESTORED, its tasks on `worker-pipeline`.
#
# The sources run outside the stack, as a team's do: each listens on this host's primary address,
# which is neither a network the stack is attached to nor a Docker gateway, so the egress proxy
# and the relay reach it the way they reach a source on the LAN. They share the host's network
# rather than publish a port: a published port reached from another Docker network would cross
# Docker's isolation between bridge networks. (A production host puts its own address in
# LAKANA_EGRESS_REFUSED, which would refuse these; this one must not.)
examples="$here/../docs/examples"
host_ip=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
if [ -z "$host_ip" ]; then echo "cannot tell this host's primary IPv4 address (ip route get 1.1.1.1)" >&2; exit 1; fi
python3 - "$host_ip" "$(sed -n "s/^LAKANA_EGRESS_REFUSED='\(.*\)'$/\1/p" .env)" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
for item in filter(None, (part.strip() for part in sys.argv[2].split(","))):
    if address in ipaddress.ip_network(item, strict=False):
        raise SystemExit(f"LAKANA_EGRESS_REFUSED refuses {address}, where the sources listen")
PY
sources="$work/sources"
mkdir -p "$sources/api"
# One CA per run, and a certificate for this host's address that both sources serve.
python3 "$examples/ingest-rest/make_certs.py" "$sources/tls" "$host_ip"
secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")'; }

# The SQL source: PostgreSQL 18 with TLS, a read-only login, three orders. Order 3 is missing: its
# id was taken, but its transaction commits only after the first run (the template reads again
# behind the largest id).
postgres_image="postgres:18.6@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6"
docker pull -q "$postgres_image" >/dev/null
db="$project_name-source-db"
db_login=$(secret)
db_port=$(python3 -c 'import socket,sys; s=socket.socket(); s.bind((sys.argv[1], 0)); print(s.getsockname()[1])' "$host_ip")
# The key must belong to the server's user (uid 999) with mode 0600: copied before the start.
docker create --name "$db" --label "lakana.smoke.source=$project_name" --network host \
  -e POSTGRES_PASSWORD="$(secret)" -e POSTGRES_DB=shop -e PGPORT="$db_port" \
  --entrypoint bash "$postgres_image" -c \
  'install -d -o 999 -g 999 -m 0700 /etc/pg-ssl && install -o 999 -g 999 -m 0600 /certs/server.pem /certs/server-key.pem /etc/pg-ssl/ && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/etc/pg-ssl/server.pem -c ssl_key_file=/etc/pg-ssl/server-key.pem -c "listen_addresses=$0"' \
  "$host_ip" >/dev/null
docker cp "$sources/tls/." "$db:/certs"
docker start "$db" >/dev/null
# On the host's address: the image's first, temporary server answers on its socket only.
for _ in $(seq 1 60); do docker exec "$db" pg_isready -q -h "$host_ip" -U postgres -d shop && break; sleep 1; done
docker exec "$db" pg_isready -q -h "$host_ip" -U postgres -d shop
orders() { docker exec -i "$db" psql -q -v ON_ERROR_STOP=1 -U postgres -d shop; }
orders <<SQL
CREATE ROLE reader LOGIN PASSWORD '$db_login';
CREATE TABLE orders (id bigint PRIMARY KEY, customer text NOT NULL, amount numeric(10, 2) NOT NULL);
GRANT SELECT ON orders TO reader;
INSERT INTO orders VALUES (1, 'Ada', 12.50), (2, 'Grace', 8.00), (4, 'Ada', 5.25);
SQL

# The REST source: the how-to's stand-in (bearer token, pages, TLS 1.2 or newer) in a container of
# the task image, three items.
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$sources/api/token.txt"
cp "$sources/tls/server.pem" "$sources/tls/server-key.pem" "$examples/ingest-rest/stand_in.py" "$sources/api/"
cp "$examples/ingest-rest/items-1.json" "$sources/api/items.json"
docker run -d --name "$project_name-source-api" --label "lakana.smoke.source=$project_name" \
  --network host --user "$(id -u):$(id -g)" -v "$sources/api:/api" --entrypoint python \
  "$TASK_IMAGE-py$python" /api/stand_in.py /api "$host_ip" 0 >/dev/null
for _ in $(seq 1 30); do [ -s "$sources/api/port" ] && break; sleep 1; done
if [ ! -s "$sources/api/port" ]; then echo "the REST source did not start listening" >&2; exit 1; fi
api_port=$(tr -d '[:space:]' < "$sources/api/port")

# The file source: a directory of exports on this host, two files written on 1 October, readable
# by the task's user; the bundle mounts it read-only as the ingestion root `exports`.
landing="$work/landing"
mkdir -p "$landing/orders"
cp "$examples/ingest-files/exports/orders-1.csv" "$examples/ingest-files/exports/orders-2.csv" "$landing/orders/"
touch -d '2026-10-01 08:00:00 UTC' "$landing/orders/"*.csv
chmod 755 "$landing" "$landing/orders"
chmod 644 "$landing/orders/"*.csv
python3 - "$landing" <<'PY'
import json
import pathlib
import sys

env = pathlib.Path(".env")
lines = env.read_text().splitlines()
setting = "LAKANA_INGESTION_ROOTS"
assert sum(line.startswith(setting + "=") for line in lines) == 1, f".env has no {setting}"
line_of_roots = setting + "='" + json.dumps({"exports": sys.argv[1]}) + "'"
env.write_text(
    "\n".join(line_of_roots if line.startswith(setting + "=") else line for line in lines) + "\n"
)
PY
# The launcher reads its roots when it starts: this recreates it with the new value.
docker compose up -d --wait --wait-timeout 180

# The connections, each checked by `lakana connection test` in a check container through the
# egress proxy or the relay; the secrets are made here and go in through stdin.
lakana connection create exports --env dev --kind directory --root exports --path orders >/dev/null
printf '%s' "$db_login" | lakana connection create source-db --env dev --kind postgresql \
  --host "$host_ip" --port "$db_port" --database shop --user reader --tls verify-full \
  --ca-file "$sources/tls/ca.pem" --password-stdin >/dev/null
lakana connection create source-api --env dev --kind http --url "https://$host_ip:$api_port/api" \
  --auth bearer --ca-file "$sources/tls/ca.pem" --password-stdin < "$sources/api/token.txt" >/dev/null
for connection in exports source-db source-api; do
  out=$(lakana connection test "$connection" --env dev) || { echo "$connection: $out" >&2; exit 1; }
  if grep -q "credentials were not checked" <<<"$out"; then echo "$connection: $out" >&2; exit 1; fi
done

# The project: the three template assets, copied into one project, and a fourth that tries a host
# none of its connections names. httpx does not show the proxy's answer to a refused CONNECT, so
# the asset asks the proxy again and fails with its reason.
ingest="$PWD/repos/ingest"
lakana init "$ingest" --template ingest-sql >/dev/null
for template in ingest-files ingest-rest; do
  lakana init "$work/$template" --template "$template" >/dev/null
  cp "$work/$template"/assets/bronze/*.py "$ingest/assets/bronze/"
done
cat > "$ingest/assets/bronze/elsewhere.py" <<'PY'
"""Tries a host that none of its connections names: the egress proxy refuses it."""

import base64
import http.client
import os
from urllib.parse import urlsplit

import httpx

from lakana import asset


@asset(connections=["source-api"], retries=0)
def elsewhere():
    """Fail with the egress proxy's reason for refusing example.org."""
    proxy = os.environ["LAKANA_EGRESS_PROXY"]
    try:
        httpx.get("https://example.org/", proxy=proxy, timeout=10)
    except httpx.ProxyError as exc:
        url = urlsplit(proxy)
        login = base64.b64encode(f"{url.username}:{url.password}".encode()).decode()
        tunnel = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
        tunnel.request(
            "CONNECT",
            "example.org:443",
            headers={"Host": "example.org:443", "Proxy-Authorization": f"Basic {login}"},
        )
        reason = tunnel.getresponse().read().decode()
        raise RuntimeError(f"httpx: {exc}; the proxy said: {reason}") from None
    raise RuntimeError("the egress proxy let a host through that no connection names")
PY
# The project's pyproject and wheels, as the demo project's; it needs neither of the demo's assets.
bash "$here/python_asset_project.sh" "$ingest" "$wheelhouse" "$python"
rm "$ingest/assets/bronze/example.sql"
rm -r "$ingest/assets/silver"
cat >> "$ingest/lakana.yaml" <<'YAML'
jobs:
  ingest:
    select: ["bronze.exports", "bronze.orders", "bronze.items"]
    schedule:
      cron: "* * * * *"
YAML
git -C "$ingest" init -q
git -C "$ingest" -c user.name=smoke -c user.email=smoke@lakana.invalid add -A
git -C "$ingest" -c user.name=smoke -c user.email=smoke@lakana.invalid commit -qm "Ingest three sources"
lakana repo add ingest --kind local --url /repos/ingest >/dev/null
out=$(lakana --output json deploy ingest --env dev)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["status"]=="succeeded", d' "$out"
# The schedule starts once a run principal is bound (how-to "Run a job on a schedule").
lakana principal create ingest-runner --type service-account >/dev/null
lakana grant create --principal ingest-runner --role DATA_ENGINEER --kind ENVIRONMENT --env dev >/dev/null
lakana deployments set-run-as ingest --env dev --principal ingest-runner >/dev/null

# Waits at most four minutes for a run of job `ingest` that its schedule started and that is not
# run $1, pauses the job at once (so no later minute starts a second run), waits for the run to
# end and sets run_id. The run must have succeeded, as the bound principal.
wait_for_scheduled_run() {
  local before=$1 deadline=$((SECONDS + 240)) status=""
  run_id=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    out=$(lakana --output json runs list --env dev --job ingest --trigger schedule --limit 20)
    run_id=$(python3 -c 'import json,sys; print(next((r["id"] for r in json.loads(sys.argv[1]) if r["id"] != sys.argv[2]), ""))' "$out" "$before")
    if [ -n "$run_id" ]; then break; fi
    sleep 3
  done
  if [ -z "$run_id" ]; then echo "no scheduled run of job ingest started within 4 minutes" >&2; lakana job show ingest --env dev --repo ingest >&2; return 1; fi
  lakana job pause ingest --env dev --repo ingest >/dev/null
  deadline=$((SECONDS + 240))
  while [ "$SECONDS" -lt "$deadline" ]; do
    out=$(lakana --output json runs show "$run_id" --env dev)
    status=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["status"])' "$out")
    case "$status" in succeeded|failed|cancelled) break ;; esac
    sleep 3
  done
  if [ "$status" != "succeeded" ]; then
    echo "scheduled run $run_id is $status: $out" >&2
    lakana runs logs "$run_id" --env dev >&2 || true
    return 1
  fi
  python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert (r["trigger"], r["job"], r["run_as"]) == ("schedule", "ingest", "ingest-runner"), r' "$out"
}
counts() {
  out=$(lakana --output json sql "SELECT (SELECT count(*) FROM bronze.exports) AS files, (SELECT count(*) FROM bronze.orders) AS orders, (SELECT count(*) FROM bronze.items) AS items")
  python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [json.loads(sys.argv[2])], sys.argv[1]' "$out" "$1"
}
wait_for_scheduled_run ""
first=$run_id
counts '{"files": 3, "orders": 3, "items": 3}'

# New data in each source: a third export written on 2 October; order 3 commits late and order 5
# is new; one item changes and one is added. The next scheduled run reads only what is new.
cp "$examples/ingest-files/exports/orders-3.csv" "$landing/orders/"
touch -d '2026-10-02 08:00:00 UTC' "$landing/orders/orders-3.csv"
chmod 644 "$landing/orders/orders-3.csv"
orders <<'SQL'
INSERT INTO orders VALUES (3, 'Linus', 20.00), (5, 'Barbara', 9.75);
SQL
cp "$examples/ingest-rest/items-2.json" "$sources/api/items.json"
lakana job resume ingest --env dev --repo ingest >/dev/null
wait_for_scheduled_run "$first"
counts '{"files": 5, "orders": 5, "items": 4}'
out=$(lakana --output json sql "SELECT id, count(*) AS n FROM bronze.orders GROUP BY id ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["n"]) for r in rows] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)], rows' "$out"
out=$(lakana --output json sql "SELECT id, name FROM bronze.items ORDER BY id")
python3 -c 'import json,sys; rows=json.loads(sys.argv[1]); assert [(r["id"], r["name"]) for r in rows] == [(1, "Anchor"), (2, "Buoy, large"), (3, "Cleat"), (4, "Deck mop")], rows' "$out"

# Lineage reaches the PostgreSQL connection: deploy → scheduled run → CONNECTION node.
out=$(lakana table lineage bronze.orders --env dev)
if ! grep -Fq "connection source-db (postgresql)" <<<"$out"; then echo "bronze.orders' lineage: $out" >&2; exit 1; fi

# The fourth asset: the proxy refuses a host its connections do not name, with the task's hint.
if lakana run ingest --env dev --select bronze.elsewhere >/dev/null 2>&1; then
  echo "bronze.elsewhere succeeded: the egress proxy let an undeclared host through" >&2
  exit 1
fi
out=$(lakana --output json runs list --env dev --limit 1)
blocked=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])[0]["id"])' "$out")
logs=$(lakana runs logs "$blocked" --env dev)
if ! grep -Fq "example.org:443 is not a destination this task may reach" <<<"$logs"; then
  echo "the refused run's log does not show the proxy's reason: $logs" >&2
  exit 1
fi

# A local file loads into bronze with one command.
printf 'id,reading\n1,20.5\n2,21.0\n' > "$work/first.csv"
lakana load "$work/first.csv" bronze.first_file --env dev --yes >/dev/null
out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.first_file")
python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 2}], sys.argv[1]' "$out"
echo "compose smoke test: OK"
