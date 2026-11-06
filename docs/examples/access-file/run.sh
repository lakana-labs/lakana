#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Keep access in a reviewed file". The page shows the regions; the
# lines between regions are the checks. The local instance is this example's own, so the file may
# manage all of it. After the account is made, the commands run with that account's token, as the
# workflow of the page does. Run by scripts/run_docs_examples.sh, which starts the local instance
# and sets EXAMPLE_DIR and WORK.
cd "$WORK"
export COLUMNS=200

captured=""
capturing=""
capture_start() {
  rm -f "$captured"
  captured=$(mktemp)
  capturing=yes
  exec 3>&1 4>&2 >"$captured" 2>&1
}
capture_end() {
  exec >&3 2>&4 3>&- 4>&-
  capturing=""
  cat "$captured"
}
# If a command fails inside a capture, still show what it printed. Always remove the file.
trap '[ -z "$capturing" ] || { exec >&3 2>&4; cat "$captured"; }; rm -f "$captured"' EXIT

# What the page assumes: a table, a person and a group.
lakana table create gold.customers --column id:long --column email:string >/dev/null
lakana principal create alice --type user >/dev/null 2>&1
lakana group create finance >/dev/null

capture_start
# docs:start account
lakana principal create access-admin --type service-account
lakana grant create --principal access-admin --role ADMIN --kind INSTANCE
lakana token issue access-admin access-ci
# docs:end account
capture_end
TOKEN=$(sed -n 's/^Token: //p' "$captured")
case "$TOKEN" in lakana_pat_*) ;; *) echo "no token in the output" >&2; exit 1 ;; esac

# From here on the commands run as that account, with a configuration folder of its own.
URL=$(lakana --output json profile list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["url"])')
export XDG_CONFIG_HOME="$WORK/access-admin-config"
mkdir -p "$XDG_CONFIG_HOME"
printf '%s\n' "$TOKEN" | lakana login --url "$URL" --env dev --token-stdin >/dev/null
lakana whoami | grep -Fq access-admin

capture_start
# docs:start export
lakana access export -o access.yaml
# docs:end export
capture_end
grep -Fxq 'manages:' access.yaml
grep -Fxq -- '- instance' access.yaml
grep -Fxq -- '- access-admin' access.yaml

# The edit the page describes: alice joins finance, finance may read dev, and sees no e-mail.
python3 - <<'PY'
from pathlib import Path

path = Path("access.yaml")
text = path.read_text().replace("members: []", "members: [alice]")
text += "- to: group:finance\n  role: ANALYST\n  on: dev\n"
text += "policies:\n- kind: column_mask\n  table: dev.gold.customers\n  column: email\n"
text += "  expression: \"'***'\"\n  to: group:finance\n"
path.write_text(text)
PY

capture_start
# docs:start plan
lakana access plan access.yaml
# docs:end plan
capture_end
grep -Fq '+ member alice of group finance' "$captured"
grep -Eq '^\+ column mask on dev.gold.customers.email' "$captured"

# The stricter way applies from a terminal and asks; the runner has none, so give it one.
lakana() {
  if [ "$1" = access ] && [ "$2" = apply ]; then
    printf 'y\n' | python3 -c '
import os, pty, sys
sys.exit(os.waitstatus_to_exitcode(pty.spawn(sys.argv[1:])))' "$(type -P lakana)" "$@"
  else
    "$(type -P lakana)" "$@"
  fi
}
capture_start
# docs:start stricter
lakana access apply access.yaml --source-commit 0123abc
# docs:end stricter
capture_end
grep -Fq 'Apply these changes?' "$captured"
grep -Fq 'Applied: 3 created, 0 changed, 0 removed.' "$captured"
unset -f lakana

# The workflow's apply of the merged file: nothing is left to change.
lakana access apply access.yaml --yes --source-commit 0123abc | grep -Fq 'Applied: 0 created'

capture_start
# docs:start status
lakana access status
# docs:end status
capture_end
grep -Fq 'access-admin' "$captured"
grep -Fq '0123abc' "$captured"

# The refusal is the point; keep the line tracer's note out of the captured output.
set +e
trap '' ERR
capture_start
# docs:start refused
lakana grant create --principal alice --role viewer --kind ENVIRONMENT --env dev
# docs:end refused
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
grep -Fq 'is managed by an access file' "$captured"

capture_start
# docs:start override
lakana grant create --principal alice --role viewer --kind ENVIRONMENT --env dev --override-managed --reason "Incident 42 hotfix"
# docs:end override
capture_end
grep -Fq 'Overriding a scope an access file manages' "$captured"

capture_start
# docs:start drift
lakana access plan access.yaml
# docs:end drift
capture_end
grep -Fq '! drift in instance: grant VIEWER to alice (override)' "$captured"
