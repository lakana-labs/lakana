#!/usr/bin/env bash
set -euo pipefail
# The command of the upgrade note "Control characters in asset files are refused". The page shows
# the region. Not an example the runner starts (it searches the reader's own assets/ folder):
# scripts/tests/test_find_control_characters.py runs it on sample files.

# docs:start find
find assets -type f -exec perl -CSD -ne '
  printf "%s:%d: control character U+%04X\n", $ARGV, $., ord $& if /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\r(?!\n)/;
  close ARGV if eof' {} +
# docs:end find
