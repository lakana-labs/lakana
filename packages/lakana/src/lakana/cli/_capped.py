"""Read a local Parquet or Arrow file in a child process whose memory is capped.

A small Parquet or Arrow file can decode to far more than its size, and its metadata can say
otherwise, so no estimate made before reading bounds what one batch takes. The child process
that reads it therefore gets an operating-system limit on its private writable memory
(`RLIMIT_DATA`): what it holds once its imports are done, plus twice the upload limit (the rows
kept, and one batch being decoded), plus `HEADROOM`. A file that needs more fails to allocate
there and is refused as larger than one upload; the CLI's own process is never capped, so a
refusal cannot break anything else. The rows come back as an Arrow stream on the child's
standard output.

`RLIMIT_DATA` (Linux 4.7 and later) counts memory written, not address space reserved, so the
memory-mapped Arrow file and the 64 MiB a malloc arena reserves per thread do not count; under
`RLIMIT_AS` they did, and ordinary wide files were refused on machines with many cores. The
child also uses the system allocator, at most two malloc arenas and `THREADS` threads per pyarrow
pool, so what it holds does not depend on the machine.

Only Linux enforces the limit. Elsewhere, and where `/proc` cannot be read, the file is read
without a cap, bounded by its metadata and the decoded size of each batch.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this interpreter on this module, without a shell
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

from lakana.cli import _output
from lakana.cli._state import CliError

HEADROOM = 256 * 1024 * 1024
"""Private writable memory the child may hold beyond twice the limit, for decoding.

Measured under a 256 MiB limit, as the least memory beyond the child's own that let the read
pass: a 248 MiB Parquet table and a 233 MiB table of text 352 MiB (320 MiB failed), a 180 MiB
table of 48 columns in 30 row groups 256 MiB, a 247 MiB Arrow file 224 MiB or less. The cap
there is 768 MiB, 416 MiB above the largest, whatever the thread count (128 asked for)."""
THREADS = 2
"""Threads in each of the child's pyarrow pools, so its memory does not grow with the cores."""
REFUSED = 3
"""The child's exit code for a refusal, described as JSON on its standard error."""
OUT_OF_MEMORY = 4
"""The child's exit code when the read ran out of the memory it may take."""
_LOADER = 127
"""The exit code of the C library's loader when it aborts, e.g. out of thread-local memory."""
_CAPPED = frozenset({"parquet", "arrow"})
"""Formats read in the child; text is checked against the limit by its size before."""


def enforced() -> bool:
    """Say whether this system enforces a limit on a process's memory.

    Returns
    -------
    bool
        True on Linux; macOS and Windows do not enforce `RLIMIT_DATA`.
    """
    return sys.platform == "linux"


def read(
    path: Path, fmt: str, *, sheet: str | None, header_row: int, limit: int | None
) -> pa.Table:
    """Read a local file, a Parquet or Arrow file in a child process with capped memory.

    Parameters
    ----------
    path
        The file.
    fmt
        Its format.
    sheet
        `--sheet`.
    header_row
        `--header-row`.
    limit
        The server's upload limit, or `None` when it does not say (nothing to cap to).

    Returns
    -------
    pa.Table
        The rows, as `lakana.cli.load.read_file` returns them.

    Raises
    ------
    CliError
        As `read_file` raises it, or the size refusal (exit 2) when the child ran out of
        memory or was killed.
    """
    from lakana.cli.load import read_file

    if limit is None or fmt not in _CAPPED or not enforced():
        return read_file(path, fmt, sheet=sheet, header_row=header_row, limit=limit)
    request = {
        "path": str(path),
        "format": fmt,
        "sheet": sheet,
        "header_row": header_row,
        "limit": limit,
    }
    command = [sys.executable, "-I", "-m", "lakana.cli._capped", json.dumps(request)]
    environment = {
        **os.environ,
        "ARROW_DEFAULT_MEMORY_POOL": "system",
        "MALLOC_ARENA_MAX": "2",
        "OMP_NUM_THREADS": str(THREADS),
    }
    with subprocess.Popen(  # nosec B603 -- this interpreter and module, no shell
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment
    ) as child:
        try:
            with ipc.open_stream(child.stdout) as reader:
                table = reader.read_all()
        except (pa.ArrowInvalid, OSError):
            table = None
        stderr = child.stderr.read() if child.stderr else b""
        code = child.wait()
    if code == 0 and table is not None:
        return table
    error = failure(path, limit, code, stderr)
    raise error


def failure(path: Path, limit: int, code: int, stderr: bytes) -> CliError:
    """Describe how a child that did not return rows ended.

    Parameters
    ----------
    path
        The file.
    limit
        The upload limit.
    code
        The child's exit code; negative for the signal that ended it.
    stderr
        What it wrote to its standard error.

    Returns
    -------
    CliError
        The child's own refusal; the size refusal (exit 2) when it ran out of memory, was
        killed, or could not start a thread under the cap (the C library then aborts with exit
        127 and says so; the only case told by its words, as no type crosses a process); else an
        unreadable file (exit 8) with the last line it wrote.
    """
    from lakana.cli.load import too_large

    if code == REFUSED:
        refused = json.loads(stderr)
        return CliError(refused["message"], refused["exit_code"], kind=refused["kind"])
    thread = code == _LOADER and b"cannot allocate memory" in stderr
    if code == OUT_OF_MEMORY or code < 0 or thread:
        return too_large(path, limit)
    last = stderr.decode("utf-8", "replace").strip().rsplit("\n", 1)[-1]
    return CliError(
        f"Could not read {_output.inline(path.name)}: its reader stopped (exit {code}): "
        f"{_output.inline(last)}",
        8,
        kind="FileError",
    )


def _held() -> int | None:
    """Give the private writable memory this process holds, as `RLIMIT_DATA` counts it.

    Returns
    -------
    int or None
        `VmData` from `/proc/self/status` in bytes, or `None` where it cannot be read.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmData:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def main(argv: list[str]) -> int:
    """Read the file a request names under a memory cap and write its rows to standard output.

    Without `/proc` to measure from, no cap is set: the documented residual of other systems.

    Parameters
    ----------
    argv
        One argument: the request as JSON (path, format, sheet, header row, limit).

    Returns
    -------
    int
        0 with the rows written, `REFUSED` with the refusal as JSON on standard error, or
        `OUT_OF_MEMORY`.
    """
    import resource

    from lakana.cli.load import read_file

    request = json.loads(argv[0])
    pa.set_cpu_count(THREADS)
    pa.set_io_thread_count(THREADS)
    held = _held()
    if held is not None:
        cap = held + 2 * request["limit"] + HEADROOM
        hard = resource.getrlimit(resource.RLIMIT_DATA)[1]
        soft = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (soft, hard))
    try:
        table = read_file(
            Path(request["path"]),
            request["format"],
            sheet=request["sheet"],
            header_row=request["header_row"],
            limit=request["limit"],
        )
        with ipc.new_stream(sys.stdout.buffer, table.schema) as writer:
            writer.write_table(table)
    except CliError as exc:
        refusal = {"message": str(exc), "exit_code": exc.exit_code, "kind": exc.kind}
        sys.stderr.write(json.dumps(refusal))
        return REFUSED
    except MemoryError:
        return OUT_OF_MEMORY
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
