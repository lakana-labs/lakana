"""Fuzz the access file parser: a refusal or the parsed entries.

Run by ClusterFuzzLite (``.clusterfuzzlite/build.sh``); what it checks is
`fuzz.targets.access_file`.
Locally: ``uv run --group fuzz python -m fuzz.access_file_fuzzer -max_total_time=60``.
"""

from __future__ import annotations

import logging
import sys

import atheris

with atheris.instrument_imports(include=["lakana_core", "yaml"]):
    from fuzz.targets import access_file


def main() -> None:
    """Hand the target to Atheris; a crash ends the run and leaves the input behind."""
    # The parser's warnings would be printed once per input.
    logging.disable(logging.CRITICAL)
    atheris.Setup(sys.argv, access_file)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
