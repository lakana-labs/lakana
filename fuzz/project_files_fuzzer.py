"""Fuzz the asset front-matter and the ``lakana.yaml`` parsers: a refusal or a parsed file.

Run by ClusterFuzzLite (``.clusterfuzzlite/build.sh``); what it checks is
`fuzz.targets.project_files`.
Locally: ``uv run --group fuzz python -m fuzz.project_files_fuzzer -max_total_time=60``.
"""

from __future__ import annotations

import logging
import sys

import atheris

with atheris.instrument_imports(include=["lakana_core", "yaml"]):
    from fuzz.targets import project_files


def main() -> None:
    """Hand the target to Atheris; a crash ends the run and leaves the input behind."""
    # The parser's warnings would be printed once per input.
    logging.disable(logging.CRITICAL)
    atheris.Setup(sys.argv, project_files)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
