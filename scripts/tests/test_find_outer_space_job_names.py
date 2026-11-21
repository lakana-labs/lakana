"""The command of the upgrade note finds the job names a new deploy refuses, and nothing else."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "docs/examples/find-outer-space-job-names.sh"


def _command() -> str:
    """Read the `find` region of the example.

    Returns
    -------
    str
        The shell text between `docs:start find` and `docs:end find`.
    """
    lines = EXAMPLE.read_text().splitlines()
    start = lines.index("# docs:start find") + 1
    return "\n".join(lines[start : lines.index("# docs:end find")])


def test_the_command_lists_job_names_with_an_outer_space(tmp_path: Path) -> None:
    """Quoted and bare keys with an ASCII or no-break space are listed; clean names are not."""
    (tmp_path / "lakana.yaml").write_text(
        'repo: analytics\njobs:\n  "a ":\n    select: ["gold.*"]\n  \'b \':\n    select: []\n'
        '  \u00a0c:\n    select: []\n  " d":\n    select: []\n  ok:\n    select: []\n'
        '  "in side":\n    select: []\n',
        encoding="utf-8",
    )
    done = subprocess.run(
        ["bash", "-c", _command()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert done.stderr == ""
    assert done.stdout.splitlines() == ["3 'a '", "5 'b '", "7 '\\xa0c'", "9 ' d'"]
