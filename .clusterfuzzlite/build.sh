#!/bin/bash -eu
# Builds the harnesses in fuzz/ for ClusterFuzzLite (docs/agent/security.md, "Fuzzing"). Runs in
# the builder image of .clusterfuzzlite/Dockerfile, from $SRC/lakana.

# lakana-core's dependencies as uv.lock pins them, checked against the lock's hashes. lakana-core
# itself is not installed: PyInstaller reads it from its source tree, so no version is computed
# from git tags the build's checkout may lack.
uv export --frozen --no-dev --no-emit-workspace --package lakana-core \
  --format requirements.txt --output-file "$WORK/requirements.txt" > /dev/null
pip3 install --no-deps --require-hashes -r "$WORK/requirements.txt"

for fuzzer in fuzz/*_fuzzer.py; do
  name=$(basename -s .py "$fuzzer")
  compile_python_fuzzer "$fuzzer" --paths="$SRC/lakana" --paths="$SRC/lakana/packages/lakana-core/src"
  # Inputs up to 4 KiB: longer ones slow every run, and a query or file rarely needs more to
  # reach a path.
  printf '[libfuzzer]\nmax_len = 4096\n' > "$OUT/$name.options"
done

# <fuzzer>_seed_corpus.zip next to each fuzzer: fuzz/seeds/<name>.json, one file per seed.
python3 fuzz/seed_corpus.py --zip "$OUT"
