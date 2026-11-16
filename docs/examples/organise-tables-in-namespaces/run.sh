#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Organise tables in namespaces". The page shows the regions; the
# lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the
# local instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

paths() {
  # paths: print the namespaces of dev, one path per line.
  lakana --output json ns list --env dev \
    | python3 -c 'import json,sys; print("\n".join(entry["path"] for entry in json.load(sys.stdin)))'
}

# docs:start create
lakana ns create gold.sales --env dev
# docs:end create

paths | grep -qx 'gold.sales'

# docs:start list
lakana ns list --env dev
# docs:end list

# docs:start list-parent
lakana ns list --env dev --parent gold --depth 1
# docs:end list-parent

out=$(lakana --output json ns list --env dev --parent gold --depth 1)
python3 -c '
import json, sys
assert [entry["path"] for entry in json.loads(sys.argv[1])] == ["gold.sales"], sys.argv[1]
' "$out"

# A namespace that holds a table is refused, with the sentence the page quotes.
lakana table create gold.sales.orders --column id:long --env dev
if message=$(lakana ns delete gold.sales --env dev --yes 2>&1); then
  echo "a namespace that holds a table was deleted" >&2
  exit 1
fi
message=$(printf '%s' "$message" | tr -s '\n ' '  ')
case "$message" in
  *"gold.sales still holds 1 table."*) ;;
  *) echo "unexpected refusal: $message" >&2; exit 1 ;;
esac
lakana table delete gold.sales.orders --env dev --purge --yes

# docs:start delete
lakana ns delete gold.sales --env dev --yes
# docs:end delete

if paths | grep -qx 'gold.sales'; then
  echo "the deleted namespace is still listed" >&2
  exit 1
fi

# A protected environment of its own.
mkdir -p "$WORK/prod"
lakana env create prod --storage-root "file://$WORK/prod"
lakana env protect prod

if lakana ns create gold.finance --env prod >/dev/null 2>&1; then
  echo "a protected environment took a namespace without a reason" >&2
  exit 1
fi

# docs:start protected-create
lakana ns create gold.finance --env prod --break-glass --reason "Quarter close needs a finance namespace"
# docs:end protected-create

# docs:start protected-delete
lakana ns delete gold.finance --env prod --yes --break-glass --reason "Created in the wrong environment"
# docs:end protected-delete

out=$(lakana --output json ns list --env prod)
python3 -c '
import json, sys
assert "gold.finance" not in [entry["path"] for entry in json.loads(sys.argv[1])], sys.argv[1]
' "$out"
