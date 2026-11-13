#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "See where a table is defined". The page shows the regions; the lines
# between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK.
cd "$WORK"

git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# A project with one SQL asset, deployed from a local repository and run once. The commit's
# dates are fixed, so its id, which the page shows, is the same on every run.
mkdir -p analytics/assets/silver
cd analytics
printf 'repo: analytics\n' > lakana.yaml
cp "$EXAMPLE_DIR"/project/assets/silver/orders.sql assets/silver/
git init -q
git add -A
GIT_AUTHOR_DATE=2026-11-12T10:00:00Z GIT_COMMITTER_DATE=2026-11-12T10:00:00Z \
  git commit -q -m "One SQL asset"
lakana repo add analytics --kind local --url "$PWD" >/dev/null
lakana deploy --env dev >/dev/null
lakana run --env dev >/dev/null
first_line=$(head -n 1 assets/silver/orders.sql)

# docs:start where
lakana table definition silver.orders
# docs:end where

out=$(lakana table definition silver.orders 2>&1)
case "$out" in
  *"Defined in assets/silver/orders.sql, line 1"*"Repository  analytics"*"Commit      "*) ;;
  *) echo "unexpected location: $out" >&2; exit 1 ;;
esac
# A local repository has no git host, so there is no link to open.
case "$out" in
  *"Open "*) echo "a local repository must have no link: $out" >&2; exit 1 ;;
esac

# docs:start show
lakana table definition silver.orders --show
# docs:end show

out=$(lakana table definition silver.orders --show 2>/dev/null)
test "$(head -n 1 <<<"$out")" = "$first_line"
diff <(printf '%s\n' "$out") assets/silver/orders.sql
err=$(lakana table definition silver.orders --show 2>&1 >/dev/null)
case "$err" in
  *"Defined in assets/silver/orders.sql, line 1"*) ;;
  *) echo "unexpected location on stderr: $err" >&2; exit 1 ;;
esac
case "$err" in
  *"Open "*) echo "a local repository must have no link: $err" >&2; exit 1 ;;
esac

# A table made outside a project is not defined by one.
lakana table create silver.notes --column id:long >/dev/null

# docs:start undefined
lakana table definition silver.notes
# docs:end undefined

out=$(lakana table definition silver.notes)
test "$out" = "'silver.notes' is not defined by a project deployed to 'dev'; it was created or renamed outside a project."
