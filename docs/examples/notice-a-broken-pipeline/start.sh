#!/usr/bin/env bash
set -euo pipefail
# How the tutorial starts a local instance. The runner (scripts/run_docs_examples.sh) runs the
# command in the region below and appends `--port <free port>` (the command has no environment
# variable for the port), so that two runs never fight over the default port.

# docs:start start
lakana server start --local
# docs:end start
