#!/usr/bin/env bash
set -euo pipefail
# The command of the upgrade note "Control characters in asset files are refused". The page shows
# the region. Not an example the runner starts (it searches the reader's own assets/ folder):
# scripts/tests/test_find_control_characters.py runs it on sample files.

# docs:start find
find assets -type f \( -iname '*.sql' -o -iname '*.py' \) -exec perl -CSDA -e '
  while (<<>>) {
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\r(?!\n)/) {
      my $code = ord $&;
      (my $name = $ARGV) =~ s/([\x00-\x1f\x7f-\x9f])/sprintf "\\x%02x", ord $1/ge;
      printf "%s:%d: control character U+%04X\n", $name, $., $code;
    }
    close ARGV if eof;
  }' {} +
# docs:end find
