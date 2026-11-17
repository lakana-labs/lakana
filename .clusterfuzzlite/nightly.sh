#!/bin/bash
# The nightly fuzz run (.github/workflows/fuzz.yml, job `nightly`; docs/agent/security.md,
# "Fuzzing"): fuzzes each fuzzer ClusterFuzzLite's build_fuzzers put in OUT, starting from the
# kept corpus in CORPUS, then prunes that corpus to the inputs that add coverage.
#
# Usage: nightly.sh OUT CORPUS ARTIFACTS SECONDS RUNNER_IMAGE
#
# ClusterFuzzLite's run_fuzzers is not used here: it finds the corpus by paging through every
# artifact of the repository with the job's token, and it deletes the corpus after the run, so
# the workflow could not keep it with actions/cache. This runs the same base-runner image the
# way ClusterFuzzLite does (`run_fuzzer`, which also unpacks each fuzzer's seed corpus).
#
# A crash's input goes to ARTIFACTS/<fuzzer>-crash-<hash>; a kept input that crashes now fails that
# night's run, which runs every kept input first (the pruning then drops it). Exits 1 when a fuzzer crashed or a pruning failed,
# after every fuzzer has run; a corpus whose pruning failed is kept as it was.
set -uo pipefail
shopt -s nullglob

out=$(realpath "$1")
corpus=$(realpath -m "$2")
artifacts=$(realpath -m "$3")
seconds=$4
image=$5
mkdir -p "$corpus" "$artifacts"

run() {
  # run NAME COMMAND...: run_fuzzer's environment, OUT at /out, CORPUS at /corpus.
  local name=$1
  shift
  docker run --rm \
    -e FUZZING_ENGINE=libfuzzer -e SANITIZER=address -e FUZZING_LANGUAGE=python \
    -e ARCHITECTURE=x86_64 -e RUN_FUZZER_MODE=interactive -e CORPUS_DIR="/corpus/$name" \
    -v "$out":/out -v "$corpus":/corpus -v "$artifacts":/artifacts \
    "$image" "$@"
}

# The names go to `bash -c` as arguments, never into its command text; the `$1` in these
# scripts is the container's, hence the single quotes.
# shellcheck disable=SC2016
FRESH='rm -rf "$1" && mkdir "$1"'
# Keeps the old corpus until the pruned one is in its place, and puts it back if that fails.
# shellcheck disable=SC2016
SWAP='mv "$1" "$1.old" || exit 1
if mv "$1.pruned" "$1"; then rm -rf "$1.old"; else mv "$1.old" "$1"; exit 1; fi'

failed=0
fuzzers=0
for package in "$out"/*_fuzzer.pkg; do
  name=$(basename "$package" .pkg)
  fuzzers=$((fuzzers + 1))
  mkdir -p "$corpus/$name"
  echo "::group::Fuzz $name for $seconds s"
  run "$name" run_fuzzer "$name" -max_total_time="$seconds" -artifact_prefix="/artifacts/$name-" \
    || { echo "::error::$name crashed; its input is in the fuzz-crash-nightly artifact"; failed=1; }
  echo "::endgroup::"
  echo "::group::Prune the corpus of $name"
  # -merge=1 TARGET CORPUS: copies into TARGET only the inputs of CORPUS that add coverage.
  if run "$name" bash -c "$FRESH" _ "/corpus/$name.pruned" \
    && run "$name" run_fuzzer "$name" -merge=1 -artifact_prefix="/artifacts/$name-" \
      "/corpus/$name.pruned"; then
    run "$name" bash -c "$SWAP" _ "/corpus/$name" \
      || { echo "::error::replacing the corpus of $name failed; its old corpus is kept"; failed=1; }
  else
    echo "::error::pruning $name failed; its corpus is kept unpruned"
    run "$name" rm -rf "/corpus/$name.pruned"
    failed=1
  fi
  echo "$name keeps $(find "$corpus/$name" -type f | wc -l) inputs"
  echo "::endgroup::"
done

if [ "$fuzzers" -eq 0 ]; then
  echo "::error::no fuzzer in $out"
  exit 1
fi
exit "$failed"
