#!/usr/bin/env bash
set -euo pipefail
# How the how-to "Load data from a source" starts a local instance that may read a SQLite file: the local
# runner reads a `directory` or `sqlite-file` connection below an ingestion root, a directory the
# operator names in LAKANA_RUN__INGESTION_ROOTS. The runner (scripts/run_docs_examples.sh) runs the
# region below in bash, with the example's private HOME, and appends `--port <free port>`.

# docs:start start
LAKANA_RUN__INGESTION_ROOTS="{\"landing\": \"$HOME/landing\"}" lakana server start --local
# docs:end start
