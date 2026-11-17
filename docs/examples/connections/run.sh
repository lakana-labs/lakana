#!/usr/bin/env bash
set -euo pipefail
# The commands of the reference page "Connections" and the connection case of "Bind a secret to
# its use" in the how-to "Manage secrets". The page shows the regions; the lines between regions
# are the checks. Creating, changing and deleting a connection reaches no source, so the
# database below need not exist. The local instance's administrator may manage connections and
# secrets. Run by scripts/run_docs_examples.sh, which starts the local instance and sets
# EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=120

printf '%s\n' 'not-a-real-password' > crm-password.txt
# docs:start create
lakana connection create crm-db --env dev --kind postgresql \
  --host db.example.com --database crm --user reader --password-stdin < crm-password.txt
# docs:end create
out=$(lakana --output json connection show crm-db --env dev)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert (c["secret"], c["address"]["port"], c["settings"]["tls"]) == ("crm-db-connection", 5432, "verify-full"), c' "$out"
out=$(lakana --output json secret list --env dev)
python3 -c 'import json,sys; s={x["name"]:(x["use"],x["use_target"]) for x in json.loads(sys.argv[1])}; assert s["crm-db-connection"]==("connection","crm-db"), s' "$out"

# docs:start create-directory
lakana connection create exports --env dev --kind directory --root shared --path exports
# docs:end create-directory

# docs:start list
lakana connection list --env dev
# docs:end list
out=$(lakana --output json connection list --env dev)
python3 -c 'import json,sys; assert sorted(c["name"] for c in json.loads(sys.argv[1])) == ["crm-db", "exports"]' "$out"
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert {(x["environment"], x["last_test"], tuple(x["actions"])) for x in c} == {("dev", None, ("test", "update", "delete", "name_secret"))}, c' "$out"

# docs:start list-all
lakana connection list --all-environments
# docs:end list-all
out=$(lakana --output json connection list --all-environments)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert {("dev", "crm-db"), ("dev", "exports")} <= {(x["environment"], x["name"]) for x in c}, c' "$out"

# docs:start show
lakana connection show crm-db --env dev
# docs:end show
out=$(lakana connection show crm-db --env dev)
grep -qx 'Last test:   never tested' <<<"$out"

printf '%s\n' 'another-fake-password' > crm-password.txt
# docs:start update
lakana connection update crm-db --env dev --host db2.example.com --password-stdin < crm-password.txt
# docs:end update
out=$(lakana --output json connection show crm-db --env dev)
python3 -c 'import json,sys; c=json.loads(sys.argv[1]); assert (c["address"]["host"], c["secret"]) == ("db2.example.com", "crm-db-connection"), c' "$out"

lakana secret create crm-reader --env dev --value password=not-a-real-one
# docs:start bind
lakana secret bind crm-reader --env dev --connection crm-db
# docs:end bind
out=$(lakana --output json connection show crm-db --env dev)
python3 -c 'import json,sys; assert json.loads(sys.argv[1])["secret"] == "crm-reader"' "$out"

# docs:start delete
lakana connection delete exports --env dev --yes
# docs:end delete
out=$(lakana --output json connection list --env dev)
python3 -c 'import json,sys; assert [c["name"] for c in json.loads(sys.argv[1])] == ["crm-db"]' "$out"

# docs:start audit
lakana audit list --kind connection --env dev
# docs:end audit
out=$(lakana --output json audit list --kind connection)
python3 -c 'import json,sys; t=[e["event_type"] for e in json.loads(sys.argv[1])["items"]]; assert t == ["CONNECTION_DELETED", "CONNECTION_UPDATED", "CONNECTION_CREATED", "CONNECTION_CREATED"], t' "$out"
