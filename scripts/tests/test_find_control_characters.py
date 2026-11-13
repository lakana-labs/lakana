"""The command of the upgrade note finds what a deploy refuses, and nothing else."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "docs/examples/find-control-characters.sh"


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


def test_the_command_lists_what_a_deploy_refuses(tmp_path: Path) -> None:
    """CRLF, tabs and printable Unicode pass; every refused character is reported with its line."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "crlf.sql").write_bytes(b"SELECT\t1\r\nFROM x\r\n")
    (assets / "notes.txt").write_bytes(b"\x00\n")
    (assets / "UP.SQL").write_bytes(b"-- \x00\n")
    (assets / "ok.sql").write_bytes("SELECT 'é€日本' -- 👩‍💻\n".encode())
    (assets / "nul.sql").write_bytes(b"SELECT 1\n-- \x00\n")
    (assets / "esc.sql").write_bytes(b"SELECT 1 -- \x1b[31m\n")
    (assets / "c1.sql").write_bytes("SELECT 1\n\n-- \u0085\n".encode())
    (assets / "cr.sql").write_bytes(b"SELECT 1\rFROM x\n")
    (assets / "tail_cr.sql").write_bytes(b"SELECT 1\nFROM x\r")
    done = subprocess.run(
        ["bash", "-c", _command()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert sorted(done.stdout.splitlines()) == [
        "assets/UP.SQL:1: control character U+0000",
        "assets/c1.sql:3: control character U+0085",
        "assets/cr.sql:1: control character U+000D",
        "assets/esc.sql:1: control character U+001B",
        "assets/nul.sql:2: control character U+0000",
        "assets/tail_cr.sql:2: control character U+000D",
    ]
    assert done.stderr == ""


def test_a_file_name_is_never_run_or_printed_raw(tmp_path: Path) -> None:
    """A name ending in a pipe is read as a file, and a control character in a name is escaped."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "x|.sql").write_bytes(b"-- \x00\n")
    (assets / "echo PWNED >&2 |.py").write_bytes(b"SELECT 1\n")
    (assets / "bad\x1bname.sql").write_bytes(b"-- \x00\n")
    (assets / "é.sql").write_bytes(b"-- \x00\n")
    done = subprocess.run(
        ["bash", "-c", _command()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert done.stderr == ""
    assert sorted(done.stdout.splitlines()) == [
        "assets/bad\\x1bname.sql:1: control character U+0000",
        "assets/x|.sql:1: control character U+0000",
        "assets/é.sql:1: control character U+0000",
    ]
