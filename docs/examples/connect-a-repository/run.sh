#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Connect a repository" that need no git host: registering an ssh
# repository with confirmed host keys, making its deploy keys, replacing one, and the steps after
# a leaked key. The page shows the regions; the lines between regions are the checks. Fetching
# the host keys and testing access send a worker to the reader's own git host, so the
# known_hosts file is written here as `repo host-keys --save` writes it. Run by
# scripts/run_docs_examples.sh, which starts the local instance (one environment, dev) and sets
# EXAMPLE_DIR and WORK.
cd "$WORK"

printf 'git.example.com ssh-ed25519 %s\n' \
  AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR > known_hosts

# docs:start register
lakana repo add analytics --kind ssh --url git@git.example.com:acme/analytics.git \
    --known-hosts-file known_hosts
lakana repo deploy-key analytics
# docs:end register
out=$(lakana --output json repo show analytics)
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["credential_secret"]=="analytics-deploy-key", r; k=r["deploy_keys"]; assert [x["environment"] for x in k]==["dev"], k; assert k[0]["public_key"].startswith("ssh-ed25519 "), k' "$out"
first=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["deploy_keys"][0]["fingerprint"])' "$out")
again=$(lakana --output json repo deploy-key analytics)
python3 -c 'import json,sys; a=json.loads(sys.argv[1]); assert [(k["environment"],k["made"]) for k in a["keys"]]==[("dev",False)], a' "$again"

# docs:start replace
lakana repo deploy-key analytics --environment dev --replace
# docs:end replace
out=$(lakana --output json repo show analytics)
python3 -c 'import json,sys; k=json.loads(sys.argv[1])["deploy_keys"]; assert k[0]["fingerprint"]!=sys.argv[2], k' "$out" "$first"

# docs:start leaked
lakana secret revoke analytics-deploy-key --env dev --yes
lakana repo deploy-key analytics --secret analytics-deploy-key-2
# docs:end leaked
out=$(lakana --output json repo show analytics)
python3 -c 'import json,sys; r=json.loads(sys.argv[1]); assert r["credential_secret"]=="analytics-deploy-key-2", r; assert [x["environment"] for x in r["deploy_keys"]]==["dev"], r' "$out"
