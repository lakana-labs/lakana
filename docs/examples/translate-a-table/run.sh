#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-to "Translate labels and descriptions". The page shows the regions; the
# lines between regions are the checks. Run by scripts/run_docs_examples.sh, which starts the local
# instance and sets EXAMPLE_DIR and WORK. It starts in WORK, a folder of the private home the
# runner sets.
cd "$WORK"
# The page shows tables as a terminal of 100 columns draws them.
export COLUMNS=100

field() {
  # field JSON_TEXT PYTHON_EXPRESSION_OF_t: print one value of a command's JSON output.
  python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"
}

# The page quotes the output of several commands. Each is run as it is shown; its output is
# written to a file between capture_start and capture_end, printed again afterwards, and checked.
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

# A project in English with one gold table, and French labels for the table and one column.
cd ~
lakana init shop --no-sample >/dev/null
cd shop
cp -R "$EXAMPLE_DIR/project/." .
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git init -q
git add -A
git commit -q -m "Sales, with French labels"
lakana repo add shop --kind local --url "$PWD" >/dev/null

capture_start
# docs:start deploy
lakana deploy --env dev
# docs:end deploy
capture_end
grep -Eq '^Deployed shop in dev at [0-9a-f]{40}$' "$captured"
grep -Fxq 'hint: translations/fr.yaml: 2 of 5 texts translated; the other 3 show in `en`' "$captured"
lakana run --env dev >/dev/null

# Read the table in French: the two translated labels show in French, the rest in English, marked.
capture_start
# docs:start get-fr
lakana table get gold.sales --env dev --lang fr
# docs:end get-fr
capture_end
grep -Fxq 'Label: Ventes par magasin' "$captured"
grep -Fxq 'Translations: fr 2 of 5' "$captured"
out=$(lakana --output json table get gold.sales --env dev --lang fr)
test "$(field "$out" '(t["label"], t["label_language"], t["description_language"])')" = "('Ventes par magasin', 'fr', 'en')"
test "$(field "$out" '[(c["label"], c["label_language"]) for c in t["columns"] if c["name"] == "store_id"][0]')" = "('Magasin', 'fr')"

# Find the table by its French label: the matched column says which language matched.
capture_start
# docs:start search
lakana search "Ventes" --env dev --lang fr
# docs:end search
capture_end
grep -Eq 'gold\.sales .* label \[fr\]' "$captured"
out=$(lakana --output json search "Ventes" --env dev --lang fr)
test "$(field "$out" '[(h["name"], h["matched"], h["language"]) for h in t["results"]]')" = "[('gold.sales', 'label', 'fr')]"
# Without French, the French label is not searched; the English one always is.
out=$(lakana --output json search "Ventes" --env dev --lang en)
test "$(field "$out" 't["results"]')" = "[]"
out=$(lakana --output json search "per store" --env dev)
test "$(field "$out" '[(h["name"], h["matched"], h["language"]) for h in t["results"]]')" = "[('gold.sales', 'label', 'en')]"

# Set your own language once: every command then shows French where there is a French text.
capture_start
# docs:start me
lakana me set --language fr
lakana table get gold.sales --env dev
# docs:end me
capture_end
grep -Fxq 'Your language is now fr' "$captured"
grep -Fxq 'Label: Ventes par magasin' "$captured"
out=$(lakana --output json whoami)
test "$(field "$out" 't["language"]')" = "fr"

# Write the French description by hand. A deploy never replaces it.
capture_start
# docs:start edit
lakana table describe gold.sales --env dev --lang fr --text "Une ligne par magasin."
# docs:end edit
capture_end
grep -Fxq 'Described gold.sales in dev (fr)' "$captured"
out=$(lakana --output json table get gold.sales --env dev)
test "$(field "$out" '(t["description"], t["description_source"], t["description_language"])')" = "('Une ligne par magasin.', 'edited', 'fr')"
lakana deploy --env dev >/dev/null
out=$(lakana --output json table get gold.sales --env dev)
test "$(field "$out" 't["description"]')" = "Une ligne par magasin."
# The English text is unchanged.
out=$(lakana --output json table get gold.sales --env dev --lang en)
test "$(field "$out" '(t["description"], t["description_language"])')" = "('One row per store, with its net sales.', 'en')"
lakana me set --reset >/dev/null

# A key that names a table the project does not build refuses the deploy, with the fix.
cp "$EXAMPLE_DIR/refused/translations/fr.yaml" translations/fr.yaml
git add -A
git commit -q -m "A misspelt table"
trap - ERR
set +e
capture_start
# docs:start refused
lakana deploy --env dev
# docs:end refused
status=$?
capture_end
set -e
trap 'echo "run.sh line ${LINENO}: ${BASH_COMMAND}" >&2' ERR
test "$status" -ne 0
# The terminal wraps the message; join its lines before looking for it.
tr -d '\n' <"$captured" | grep -Fq '`gold.sale` is not a table this project builds; did you mean `gold.sales`? Fix the name, or remove the entry'
