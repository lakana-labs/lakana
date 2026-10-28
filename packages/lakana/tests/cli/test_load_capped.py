"""Tests for reading a local file in a child process with capped memory (`lakana.cli._capped`)."""

from __future__ import annotations

import io
import json
import resource
import signal
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc
import pyarrow.parquet as pq
import pytest

import lakana.cli.load as load_module
from lakana.cli import _capped
from lakana.cli._state import CliError

_MIB = 1024 * 1024

linux = pytest.mark.skipif(sys.platform != "linux", reason="the cap is enforced on Linux only")


@linux
def test_a_file_read_in_the_capped_process_comes_back_whole(tmp_path: Path) -> None:
    """The child reads the file and streams the rows back unchanged."""
    path = tmp_path / "small.parquet"
    rows = pa.table({"id": [1, 2, 3], "name": ["a", None, "c"]})
    pq.write_table(rows, path)

    read = _capped.read(path, "parquet", sheet=None, header_row=1, limit=256 * _MIB)

    assert read.equals(rows)


@linux
def test_a_refusal_in_the_capped_process_keeps_its_message_and_exit_code(tmp_path: Path) -> None:
    """A file the reader refuses is refused as it would be in the CLI's own process."""
    path = tmp_path / "nested.parquet"
    pq.write_table(pa.table({"l": [[1, 2]]}), path)

    with pytest.raises(CliError) as caught:
        _capped.read(path, "parquet", sheet=None, header_row=1, limit=256 * _MIB)

    assert caught.value.exit_code == 8
    assert caught.value.kind == "FileError"
    assert "nested.parquet: Column 'l' has an unsupported Arrow type" in str(caught.value)


@linux
def test_a_file_that_needs_more_memory_than_the_cap_ends_in_the_size_refusal(
    tmp_path: Path,
) -> None:
    """A few KB of Parquet that decode to 3.5 GiB in one batch hit the cap, not the machine."""
    path = tmp_path / "bomb.parquet"
    values = pa.DictionaryArray.from_arrays(
        pa.array([0] * 1024, pa.int32()), pa.array(["x" * (7 * _MIB // 2)])
    )
    pq.write_table(pa.table({"s": values}), path, compression="zstd", store_schema=False)
    assert path.stat().st_size < 64 * 1024

    with pytest.raises(CliError) as caught:
        _capped.read(path, "parquet", sheet=None, header_row=1, limit=4 * _MIB)

    assert caught.value.exit_code == 2
    assert str(caught.value) == (
        "bomb.parquet is larger than one upload may be (4 MiB) once read; load it with a Python "
        "asset and a directory or object-store connection"
    )


@pytest.mark.parametrize(
    ("code", "stderr", "exit_code", "message"),
    [
        (_capped.OUT_OF_MEMORY, b"", 2, "f`.parquet is larger than one upload may be (1 MiB) once"),
        (-signal.SIGKILL, b"", 2, "f`.parquet is larger than one upload may be (1 MiB) once"),
        (
            _capped.REFUSED,
            json.dumps({"message": "f is bad", "exit_code": 8, "kind": "FileError"}).encode(),
            8,
            "f is bad",
        ),
        (
            1,
            b"Traceback (most recent call last):\nValueError: \x1b[31mboom\n",
            8,
            "Could not read f`.parquet: its reader stopped (exit 1): ValueError: \\x1b[31mboom",
        ),
    ],
    ids=["memory-error", "killed", "refused", "crashed"],
)
def test_how_the_reader_ended_is_reported(
    code: int, stderr: bytes, exit_code: int, message: str
) -> None:
    """Running out of memory or being killed is the size refusal; a refusal keeps its own text."""
    error = _capped.failure(Path("f`.parquet"), _MIB, code, stderr)

    assert error.exit_code == exit_code
    assert message in str(error)


class _Stdout:
    """A stand-in for `sys.stdout` with only its binary `buffer`."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def test_the_child_streams_the_table_under_a_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the child: the cap is set above what is mapped now, and the rows go out as a stream."""
    path = tmp_path / "rows.parquet"
    rows = pa.table({"id": [1, 2]})
    pq.write_table(rows, path)
    caps: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, cap: caps.append(cap))
    out = _Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    request = {
        "path": str(path),
        "format": "parquet",
        "sheet": None,
        "header_row": 1,
        "limit": _MIB,
    }

    assert _capped.main([json.dumps(request)]) == 0

    (cap,) = caps
    assert cap[0] >= 2 * _MIB + _capped.HEADROOM
    assert cap[1] == resource.getrlimit(resource.RLIMIT_AS)[1]
    assert ipc.open_stream(out.buffer.getvalue()).read_all().equals(rows)


def test_the_child_keeps_a_lower_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard limit below the cap is kept: the child cannot raise it."""
    path = tmp_path / "rows.parquet"
    pq.write_table(pa.table({"id": [1]}), path)
    caps: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (1000, 1000))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, cap: caps.append(cap))
    monkeypatch.setattr(sys, "stdout", _Stdout())
    request = {
        "path": str(path),
        "format": "parquet",
        "sheet": None,
        "header_row": 1,
        "limit": _MIB,
    }

    assert _capped.main([json.dumps(request)]) == 0
    assert caps == [(1000, 1000)]


@pytest.mark.parametrize(
    ("error", "code", "stderr"),
    [
        (CliError("no", 8, kind="FileError"), _capped.REFUSED, {"message": "no", "exit_code": 8}),
        (pa.ArrowMemoryError("out of memory"), _capped.OUT_OF_MEMORY, None),
    ],
    ids=["refused", "out-of-memory"],
)
def test_the_child_reports_how_the_read_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
    stderr: dict[str, object] | None,
) -> None:
    """A refusal goes out as JSON on stderr; running out of memory as its own exit code."""
    monkeypatch.setattr(resource, "setrlimit", lambda kind, cap: None)

    def fail(*args: object, **kwargs: object) -> pa.Table:
        raise error

    monkeypatch.setattr(load_module, "_parquet", fail)
    request = {"path": str(tmp_path / "f.parquet"), "format": "parquet", "sheet": None}

    assert _capped.main([json.dumps({**request, "header_row": 1, "limit": _MIB})]) == code

    written = capsys.readouterr().err
    if stderr is None:
        assert written == ""
    else:
        assert json.loads(written).items() >= stderr.items()


@pytest.mark.parametrize(
    ("fmt", "limit", "enforced"),
    [("csv", _MIB, True), ("parquet", None, True), ("parquet", _MIB, False)],
    ids=["text", "no-limit", "not-enforced"],
)
def test_without_a_cap_to_set_the_file_is_read_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str, limit: int | None, enforced: bool
) -> None:
    """Text is bounded by its size before it is read; without a limit or a cap, read in place."""
    path = tmp_path / f"f.{fmt}"
    pq.write_table(pa.table({"id": [1]}), path) if fmt == "parquet" else path.write_text("id\n1\n")
    monkeypatch.setattr(_capped, "enforced", lambda: enforced)

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("a child was started")

    monkeypatch.setattr(_capped.subprocess, "Popen", never)

    read = _capped.read(path, fmt, sheet=None, header_row=1, limit=limit)

    assert read.column("id").to_pylist() == [1]


def test_running_out_of_memory_is_not_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader lets a memory error through, for the child to report as one."""
    path = tmp_path / "f.parquet"
    pq.write_table(pa.table({"id": [1]}), path)

    def fail(*args: object, **kwargs: object) -> pa.Table:
        raise pa.ArrowMemoryError("malloc of size 1 failed")

    monkeypatch.setattr(load_module, "_parquet", fail)

    with pytest.raises(MemoryError):
        load_module.read_file(path, "parquet", sheet=None, header_row=1, limit=_MIB)


def test_the_cap_is_enforced_on_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only Linux enforces an address-space limit; elsewhere the metadata bound remains."""
    for platform, expected in (("linux", True), ("darwin", False), ("win32", False)):
        monkeypatch.setattr(_capped.sys, "platform", platform)
        assert _capped.enforced() is expected
