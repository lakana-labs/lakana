"""Fuzz the query guard and the rewrite: SQL text in, a refusal or a rewritten query out.

Run by ClusterFuzzLite (``.clusterfuzzlite/build.sh``); what it checks is
`fuzz.targets.query_guard`.
Locally: ``uv run --group fuzz python -m fuzz.query_guard_fuzzer -max_total_time=60``.
"""

from __future__ import annotations

import logging
import sys

import atheris

with atheris.instrument_imports(include=["lakana_core", "sqlglot"]):
    from fuzz.targets import query_guard


def main() -> None:
    """Hand the target to Atheris; a crash ends the run and leaves the input behind."""
    # The parser's warnings would be printed once per input.
    logging.disable(logging.CRITICAL)
    atheris.Setup(sys.argv, query_guard)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
