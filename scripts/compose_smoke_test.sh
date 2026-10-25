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
    *) docker build -f "$here/../Dockerfile.task" --build-arg "PYTHON=$version" -t "$reference" "$here/.." ;;
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
echo "compose smoke test: OK"
