#!/usr/bin/env bash
set -euo pipefail
# The command of the upgrade note "A job name with a space at either end". The page shows the
# region. Not an example the runner starts (it reads the reader's own lakana.yaml):
# scripts/tests/test_find_outer_space_job_names.py runs it on sample files.

# docs:start find
python3 - <<'PY'
import re

key = re.compile(r"""[ \t]+(?:"([^"]*)"|'([^']*)'|([^ \t:#][^:#]*)):\s*(?:#.*)?$""")
for number, line in enumerate(open("lakana.yaml", encoding="utf-8"), 1):
    found = key.match(line.rstrip("\n"))
    name = next((g for g in found.groups() if g is not None), "") if found else ""
    if name and (name[0].isspace() or name[-1].isspace()):
        print(number, repr(name))
PY
# docs:end find
