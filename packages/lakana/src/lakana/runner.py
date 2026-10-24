"""The task harness: `python -m lakana.runner` runs one Python asset.

The worker starts this module inside the task's venv, with the unpacked
repository as working directory, and writes one JSON task description to
stdin. The harness is an ordinary SDK client holding the task's token: it
reads each declared table input through the query API, downloads each artifact
input through `client.artifacts` into the attempt's private directory, calls the
asset function, validates what it returned and uploads it: a DataFrame through
the Arrow replace route, or, for an asset declared ``output="artifact"``, the
file or directory inside `lakana.output_dir()` that it returned, through
`client.artifacts.upload`. It never touches the database or the object store,
and it never prints the token.

The description is version 2 or 3. Version 2 is version 1's keys plus ``output``
(``"table"`` or ``"artifact"``), ``input_kinds`` (every input to ``"table"`` or
``"artifact"``), ``input_versions`` (exactly the artifact inputs, each to the
version the worker resolved) and ``work_dir`` (the attempt's private directory:
absolute, owned by this user, not accessible to group or others). The harness
creates ``<work_dir>/inputs`` (0700) when an input is an artifact and
``<work_dir>/output`` (0700) for an artifact asset. Version 3 adds ``write``
(``"replace"``, ``"append"`` or ``"merge"``), ``key`` (the merge key's columns, empty
unless ``write`` is ``"merge"``), ``watermark`` (``null`` or ``{"column": str, "value":
str | null}``, the value in the worker's ``<type>:<value>`` form) and ``connections``
(each declared connection's name to an object with at least its ``kind``); the worker
sends it only to an asset that uses one of them, and a version 2 document replaces.

An append uploads with `client.tables.append` against ``expected_snapshot_id`` (the
snapshot the worker read the watermark from), except on a first run (``"none"``), which
creates the table with `client.tables.replace`; a merge always uses `client.tables.merge`,
which creates a missing table after the same key checks. An append or merge whose
function returned no rows (or no columns) on a table that exists uploads nothing and
succeeds with ``rows`` 0. The function gets an `lakana.context.AssetContext` as its
parameter ``ctx`` when it declares one (unless the project version is from before
``ctx`` was reserved and names an input so), and `lakana.context.current` holds the
attempt's client and connections until the result record is written.

The result record is one JSON object. ``{"status": "succeeded",
"snapshot_id": int | null, "rows": int}`` after a table upload, ``{"status":
"succeeded", "output": "artifact", "version": int, "manifest_sha256": str}`` after an
artifact upload; otherwise
``{"status": "failed", "error_type": str, "message": str, "retryable": bool,
"phase": "load" | "inputs" | "function" | "validate" | "upload", "conflict":
bool}``. `phase` says where the failure came from. `message` is the exception's
text, prefixed with its class name when the repository's own code raised it
(phases ``load`` and ``function``). `conflict` is true only
when the SDK's `Conflict` was raised by the upload call itself, so an exception
the user's function raises (even one named `Conflict`) never reads as one;
`error_type` is the exception's class name, for display only.

polars is imported inside functions only, so the SDK and CLI work without
the `lakana[assets]` extra.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import errno
import importlib.util
import inspect
import json
import math
import os
import re
import signal
import stat
import sys
import tempfile
import traceback
import types
import unicodedata
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TextIO, cast

import pyarrow as pa

from lakana import assets, context
from lakana._arrow import (
    cast_timestamp_ns_columns_to_us,
    normalize_view_columns,
    schema_to_columns,
)
from lakana._artifact_manifest import (
    _BY_DESCRIPTOR,
    _DIR_FLAGS,
    ManifestError,
    StoredManifestDamaged,
    StoredManifestInvalid,
    shown,
)
from lakana._models import ArtifactVersion
from lakana.assets import ASSET_ATTRIBUTE, DECIMALS_MODES, WRITE_CHOICES, AssetSpec, _unsafe
from lakana.client import Client
from lakana.context import WATERMARK_TAGS, AssetContext, WatermarkValue, decode_watermark
from lakana.errors import (
    AuthenticationError,
    Conflict,
    LakanaError,
    NotFound,
    PayloadTooLarge,
    PermissionDenied,
    QueryFailed,
    ValidationError,
)
from lakana.ingest import _connections
from lakana.ingest._errors import IngestError

if TYPE_CHECKING:
    import polars as pl  # lazy: the SDK works without the assets extra

    _DataType = pl.DataType | pl.datatypes.DataTypeClass

DESCRIPTION_VERSIONS = (2, 3)
"""The task description versions this harness reads; the worker sends 3 only when needed."""
COMPOSITE_KEY_MAX_ROWS = 5_000
"""Most rows a merge on a key of several columns takes; the server refuses more, and this
harness says so first, in words a Python asset can act on."""
MAX_INPUT_VERSION = 2**31 - 1
"""The largest artifact version a description may name (the catalog stores a 32-bit integer)."""
_SAVE_FLAGS = _DIR_FLAGS | getattr(os, "O_PATH", 0)
"""How the working directory is saved: by ``O_PATH`` where there is one, which needs no read
permission on the directory, so code that took that away cannot make the upload fail."""
_PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
"""A parameter name that may become a directory name: an ASCII identifier."""
_KINDS = ("table", "artifact")
_NOTHING_TO_CREATE = (
    "the asset returned no columns, so there is nothing to create {table} from; its source had "
    "no data yet (check the pattern or the query); the first run must find data"
)
"""Why a first append or merge without columns fails: a table needs its columns."""
_READER = (
    "the asset returned a pyarrow RecordBatchReader; read it with `.read_all()` and return the "
    "table"
)
MAX_ERROR_MESSAGE_CHARS = 2000
"""Longest `message` in a result record; the worker stores it as the task's error."""

_MODULE_NAME = "lakana_asset"
_SERVER_ADVICE = " Pass evolve_schema"
"""Where the server's schema-mismatch text starts advising an option Python assets lack."""
_INPUT_HINT = (
    " An input is read whole into memory: if it is too large, narrow it with an upstream SQL "
    "asset (for example a view) and declare that as the input."
)
_MEMORY_HINT = (
    " The worker ran out of memory reading it: narrow the input with an upstream SQL asset "
    "(for example a view) and declare that as the input, or ask the operator to raise the "
    "memory limit that the message above names."
)
"""What to do when an input read ran out of memory."""
_TASK_MEMORY_HINT = (
    " The task ran out of memory: use less data (narrow the inputs with an upstream SQL asset, "
    "or work in smaller pieces), or run it on a machine or in a container with more memory."
)
"""What to do about a `MemoryError` in user code: the machine or container had none left."""
_TYPE_ERROR_HINT = (
    "\nThis error is almost always a mistake in the asset's code, so it "
    "is not retried. Fix the function, deploy, and run again."
)
"""What to do about a `TypeError` raised by the asset's function: almost always a code mistake."""
_PERMANENT_QUERY_CAUSES = frozenset({"not_found", "permission_denied", "result_too_large"})
"""Causes of a failed input read that repeat on every attempt. The harness writes its own
`SELECT`, so `invalid_query` there is not the user's SQL and stays retryable, as does
`out_of_memory` (another attempt may find the worker less loaded), like any other, unknown or
missing cause (an older server)."""


class InvalidResult(Exception):
    """The asset returned something that cannot replace its table. Not retryable."""


class AssetLoadError(Exception):
    """The asset file cannot be turned into one asset function. Not retryable."""


class UploadRefused(Exception):
    """The server refused the upload as invalid. Not retryable: the same bytes fail again."""


class InputError(Exception):
    """An input could not be read through the query API or downloaded."""


class ArtifactInputError(InputError):
    """An artifact input could not be downloaded.

    Kept apart from a failed table read because a 404 means something else here: the
    version the worker resolved may have been pruned since (another run's producer
    committed), and the next attempt resolves again.
    """


class WorkspaceError(Exception):
    """The attempt's directories are not what the harness made. Not retryable."""


_PERMANENT_REFUSALS = (PermissionDenied, NotFound, AuthenticationError, PayloadTooLarge)
"""SDK errors that repeat on every attempt. A 413 is the operator's size limit (a manifest of
too many files, an append over its cap): the same request is refused again."""
_STORED_MANIFEST_REFUSALS = (StoredManifestInvalid, StoredManifestDamaged)
"""Why the SDK refused to download a version whose stored manifest is damaged or over a limit
it enforces: the same manifest comes back on every attempt. A downloaded *file* that does not
match (a plain `ArtifactCorrupt`) may be transient and is not listed."""

PHASES = ("load", "inputs", "function", "validate", "upload")
_USER_CODE_PHASES = ("load", "function")
"""Phases in which the repository's code runs: the message names the exception's type."""


def _retryable(phase: str, cause: BaseException) -> bool:
    """Decide whether another attempt could end differently.

    Parameters
    ----------
    phase
        Where the failure came from.
    cause
        The exception.

    Returns
    -------
    bool
        `False` for a failure that repeats: an invalid result, a file that is
        not one asset or does not compile, a missing module, a refused request, an
        input query the server says failed for good (`_PERMANENT_QUERY_CAUSES`), an artifact
        whose stored manifest the client rejects (`_STORED_MANIFEST_REFUSALS`), a 413 (the
        operator's size limit).
        A `TypeError` (or a subclass) raised out of the asset's function is a mistake in
        its code that every attempt repeats; an error of `lakana.ingest` says itself whether
        another attempt could help (`IngestError.retryable`: a connection the asset does not
        declare, or the server refused, repeats); no other exception type of the function is
        judged by its class. A 404 on the download of an artifact input is the exception
        among refused requests: the version may have been pruned since the worker resolved it, so
        another attempt, which resolves again, can end differently (the retries are
        bounded, and the worker fails an input with no live version for good).
    """
    origin = cause.__cause__ if isinstance(cause, InputError) else cause
    if phase == "function" and isinstance(cause, IngestError) and cause.retryable is not None:
        return cause.retryable
    if isinstance(origin, MemoryError) or (phase == "function" and isinstance(cause, TypeError)):
        return False
    permanent = (InvalidResult, AssetLoadError, UploadRefused, WorkspaceError)
    if isinstance(cause, permanent) or phase == "validate":
        return False
    if (
        phase == "inputs"
        and isinstance(origin, QueryFailed)
        and origin.cause in _PERMANENT_QUERY_CAUSES
    ):
        return False
    if phase == "inputs" and isinstance(origin, _STORED_MANIFEST_REFUSALS):
        return False
    if phase == "load":
        return not isinstance(cause, (SyntaxError, ModuleNotFoundError))
    if phase == "inputs" and isinstance(cause, ArtifactInputError) and isinstance(origin, NotFound):
        return True
    return not (phase in ("inputs", "upload") and isinstance(origin, _PERMANENT_REFUSALS))


class Failure(Exception):
    """A failure of one phase of the run; carries the original exception.

    Parameters
    ----------
    phase
        One of `PHASES`.
    cause
        What was raised, including `BaseException` subclasses such as
        `KeyboardInterrupt` that user code can raise.
    """

    def __init__(self, phase: str, cause: BaseException) -> None:
        super().__init__(phase)
        self.phase = phase
        self.cause = cause


class _Phase:
    """Context manager that wraps whatever its block raises in a `Failure`.

    Parameters
    ----------
    name
        One of `PHASES`.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> None:
        """Enter the phase."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback_: types.TracebackType | None,
    ) -> None:
        """Wrap an exception from the block.

        Parameters
        ----------
        exc_type
            The exception's class, if any.
        exc
            The exception, if any.
        traceback_
            Its traceback.

        Raises
        ------
        Failure
            For any exception that is not already one.
        """
        if exc is not None and not isinstance(exc, Failure):
            raise Failure(self.name, exc) from exc


@dataclass(frozen=True)
class TaskDescription:
    """What the worker tells the harness on stdin.

    Parameters
    ----------
    server_url
        Base URL of the Lakana server.
    token
        The task's bound token. Left out of `repr`.
    environment
        The environment to read from and write to.
    asset_path
        The asset file, relative to the working directory.
    table
        The asset's own dotted table name.
    inputs
        Parameter name → dotted table name.
    expected_snapshot_id
        The target's snapshot id as the worker read it, or ``"none"``.
    result_path
        Where to write the result record.
    request_timeout_seconds
        Bound of every HTTP request and of each input query's wait.
    parent_pid
        The worker's pid, when it sent one. A harness whose parent is not this
        process exits at once: the worker died before the kernel could be asked
        to kill the harness with it.
    output
        ``table`` (the function returns a DataFrame) or ``artifact`` (it writes
        into `lakana.output_dir()` and returns a path inside it).
    input_kinds
        Parameter name → ``table`` or ``artifact``; every input has an entry.
    input_versions
        Parameter name → the artifact version the worker resolved; exactly the
        artifact inputs.
    work_dir
        The attempt's private directory: ``inputs`` and ``output`` are created
        in it.
    decimals
        ``float`` or ``exact`` from the deployed manifest, or `None` when the
        worker sent none (an older worker): the decorator's own value then applies.
    write
        ``replace``, ``append`` or ``merge``; ``replace`` for a version 2 document.
    key
        The merge key's columns; empty unless `write` is ``merge``.
    watermark_column
        The asset's `watermark` column, if the worker sent one.
    watermark
        The largest value of that column the table held when the worker read it, decoded;
        `None` when there is none yet. What `ctx.watermark()` returns.
    connections
        Each declared connection's name to what the worker sent about it (its ``kind``).
    table_exists
        Whether the table existed when the worker read it: one created by a first run of zero
        rows has no snapshot (`expected_snapshot_id` ``"none"``) and still exists. Version 2
        documents leave it out; then ``"none"`` means a first run.
    """

    server_url: str
    token: str = field(repr=False)
    environment: str
    asset_path: str
    table: str
    inputs: dict[str, str]
    expected_snapshot_id: int | str
    result_path: str
    request_timeout_seconds: float
    parent_pid: int | None = None
    output: str = "table"
    input_kinds: dict[str, str] = field(default_factory=dict)
    input_versions: dict[str, int] = field(default_factory=dict)
    work_dir: str = ""
    decimals: str | None = None
    write: str = "replace"
    key: tuple[str, ...] = ()
    watermark_column: str | None = None
    watermark: WatermarkValue | None = None
    connections: dict[str, dict[str, object]] = field(default_factory=dict)
    table_exists: bool = False


def _text(document: dict[str, object], key: str) -> str:
    """Read a non-empty string field.

    Parameters
    ----------
    document
        The decoded description.
    key
        The field.

    Returns
    -------
    str
        The value.

    Raises
    ------
    ValueError
        If the field is missing, not a string, or empty.
    """
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"task description: {key} must be a non-empty string")
    return value


def _input_fields(
    document: dict[str, object], inputs: dict[str, str]
) -> tuple[dict[str, str], dict[str, int]]:
    """Check ``input_kinds`` and ``input_versions`` against the inputs.

    Parameters
    ----------
    document
        The decoded description.
    inputs
        Parameter name → dotted object name.

    Returns
    -------
    tuple
        The kinds and the versions.

    Raises
    ------
    ValueError
        If a kind or version is missing, unknown or out of range, an artifact
        parameter cannot be a directory name, or two parameters that read one
        object disagree about it.
    """
    kinds = document.get("input_kinds")
    if (
        not isinstance(kinds, dict)
        or set(kinds) != set(inputs)
        or not all(kind in _KINDS for kind in kinds.values())
    ):
        raise ValueError(
            "task description: input_kinds must map each input to 'table' or 'artifact'"
        )
    versions = document.get("input_versions")
    artifacts = {parameter for parameter, kind in kinds.items() if kind == "artifact"}
    if (
        not isinstance(versions, dict)
        or set(versions) != artifacts
        or not all(
            type(version) is int and 1 <= version <= MAX_INPUT_VERSION
            for version in versions.values()
        )
    ):
        raise ValueError(
            "task description: input_versions must give a version for each artifact input"
        )
    if not all(_PARAMETER.fullmatch(parameter) for parameter in artifacts):
        raise ValueError(
            "task description: an artifact input's parameter name must be an ASCII identifier "
            "of at most 128 characters (letters, digits and underscores), because the copy of "
            "the artifact is stored in a directory of that name"
        )
    if len({parameter.lower() for parameter in artifacts}) != len(artifacts):
        raise ValueError(
            "task description: two artifact inputs have parameter names that differ only in "
            "case; rename one, as a file system may treat them as one directory"
        )
    seen: dict[str, tuple[str, int | None]] = {}
    for parameter, name in inputs.items():
        this = (kinds[parameter], versions.get(parameter))
        if seen.setdefault(name, this) != this:
            raise ValueError(
                "task description: parameters that read the same object must agree on its kind "
                "and version"
            )
    return dict(kinds), dict(versions)


def _result_path(document: dict[str, object]) -> str:
    """Check where the result record goes.

    Parameters
    ----------
    document
        The decoded description.

    Returns
    -------
    str
        The path.

    Raises
    ------
    ValueError
        Unless it is a non-empty absolute path: the harness changes its working
        directory while it uploads, and a relative path would then mean another file.
    """
    value = _text(document, "result_path")
    if not os.path.isabs(value):
        raise ValueError("task description: result_path must be an absolute path")
    return value


def _work_dir(document: dict[str, object]) -> str:
    """Check that the attempt's directory is one this harness may trust.

    Parameters
    ----------
    document
        The decoded description.

    Returns
    -------
    str
        The path.

    Raises
    ------
    ValueError
        Unless it is an absolute path to a directory (not a link) that is owned
        by the user running the harness and not accessible to group or others.
    """
    value = _text(document, "work_dir")
    if not os.path.isabs(value):
        raise ValueError("task description: work_dir must be an absolute path")
    try:
        info = os.lstat(value)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "task description: work_dir must be an existing directory (not a link)"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("task description: work_dir must be an existing directory (not a link)")
    if info.st_uid != os.geteuid():
        raise ValueError("task description: work_dir must be owned by the user running the harness")
    if info.st_mode & 0o077:
        raise ValueError(
            "task description: work_dir must not be accessible to group or others (mode 0700)"
        )
    return value


@dataclass(frozen=True)
class _Writes:
    """How the asset writes its table, from a version 3 document.

    Parameters
    ----------
    write
        ``replace``, ``append`` or ``merge``.
    key
        The merge key's columns.
    watermark_column
        The watermark column, if any.
    watermark
        Its decoded value, if any.
    connections
        Each declared connection's routing.
    table_exists
        Whether the asset's table existed when the worker read it.
    """

    write: str = "replace"
    key: tuple[str, ...] = ()
    watermark_column: str | None = None
    watermark: WatermarkValue | None = None
    connections: dict[str, dict[str, object]] = field(default_factory=dict)
    table_exists: bool = False


def _write_fields(document: dict[str, object]) -> _Writes:
    """Check the fields version 3 adds.

    Parameters
    ----------
    document
        The decoded description.

    Returns
    -------
    _Writes
        The write mode, key, watermark and connections.

    Raises
    ------
    ValueError
        If a field is missing or has the wrong type, the key is not given exactly for a
        merge, or the watermark's value is not one the worker encodes. The message names the
        field and never contains a value.
    """
    write = document.get("write")
    if not isinstance(write, str) or write not in WRITE_CHOICES:
        raise ValueError("task description: write must be 'replace', 'append' or 'merge'")
    key = document.get("key")
    if (
        not isinstance(key, list)
        or not all(isinstance(column, str) and column and not _unsafe(column) for column in key)
        or bool(key) != (write == "merge")
    ):
        raise ValueError(
            "task description: key must name the columns of a merge, and only of a merge"
        )
    watermark = document.get("watermark")
    column: str | None = None
    value: WatermarkValue | None = None
    if watermark is not None:
        named = watermark.get("column") if isinstance(watermark, dict) else None
        raw = watermark.get("value") if isinstance(watermark, dict) else None
        try:
            if (
                not isinstance(named, str)
                or not named
                or _unsafe(named)
                or not isinstance(raw, (str, type(None)))
            ):
                raise ValueError
            column, value = named, None if raw is None else decode_watermark(raw)
        except ValueError:
            raise ValueError(
                "task description: watermark must be null or an object with a column name and "
                "an encoded value (or null)"
            ) from None
    connections = document.get("connections")
    if not isinstance(connections, dict) or not all(
        isinstance(name, str)
        and isinstance(routing, dict)
        and isinstance(routing.get("kind"), str)
        and routing["kind"]
        for name, routing in connections.items()
    ):
        raise ValueError(
            "task description: connections must map each declared connection to an object "
            "with its kind"
        )
    table_exists = document.get("table_exists")
    if not isinstance(table_exists, bool):
        raise ValueError("task description: table_exists must be true or false")
    return _Writes(
        table_exists=table_exists,
        write=write,
        key=tuple(key),
        watermark_column=column,
        watermark=value,
        connections={name: dict(routing) for name, routing in connections.items()},
    )


def parse_description(text: str) -> TaskDescription:
    """Parse and check the task description.

    Parameters
    ----------
    text
        The JSON document read from stdin.

    Returns
    -------
    TaskDescription
        The description.

    Raises
    ------
    ValueError
        If the document is not JSON (an integer of thousands of digits and a
        document nested too deeply are not), not version 2 or 3, or a field has the
        wrong type, value or relation to another. The message names the field
        and never contains a value.
    """
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError("task description: not valid JSON") from exc
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] not in DESCRIPTION_VERSIONS
    ):
        raise ValueError("task description: expected a version 2 or 3 document")
    inputs = document.get("inputs")
    if not isinstance(inputs, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in inputs.items()
    ):
        raise ValueError("task description: inputs must map parameter names to table names")
    expected = document.get("expected_snapshot_id")
    if isinstance(expected, bool) or not (isinstance(expected, int) or expected == "none"):
        raise ValueError("task description: expected_snapshot_id must be an id or 'none'")
    timeout = document.get("request_timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("task description: request_timeout_seconds must be a positive number")
    parent = document.get("parent_pid")
    if parent is not None and (
        isinstance(parent, bool) or not isinstance(parent, int) or parent < 1
    ):
        raise ValueError("task description: parent_pid must be a positive integer")
    output = document.get("output")
    if output not in _KINDS or not isinstance(output, str):
        raise ValueError("task description: output must be 'table' or 'artifact'")
    kinds, versions = _input_fields(document, inputs)
    decimals = document.get("decimals")
    if decimals is not None and decimals not in DECIMALS_MODES:
        raise ValueError("task description: decimals must be 'float' or 'exact'")
    writes = _write_fields(document) if document["version"] == 3 else _Writes()
    return TaskDescription(
        server_url=_text(document, "server_url"),
        token=_text(document, "token"),
        environment=_text(document, "environment"),
        asset_path=_text(document, "asset_path"),
        table=_text(document, "table"),
        inputs=dict(inputs),
        expected_snapshot_id=expected,
        result_path=_result_path(document),
        request_timeout_seconds=float(timeout),
        parent_pid=parent,
        output=output,
        input_kinds=kinds,
        input_versions=versions,
        work_dir=_work_dir(document),
        decimals=decimals,
        write=writes.write,
        key=writes.key,
        watermark_column=writes.watermark_column,
        watermark=writes.watermark,
        connections=writes.connections,
        table_exists=writes.table_exists,
    )


def _quoted(table: str) -> str:
    """Quote a dotted table name for SQL.

    Parameters
    ----------
    table
        ``namespace.table``; no component contains a dot.

    Returns
    -------
    str
        Each component in double quotes, embedded quotes doubled.
    """
    return ".".join('"' + part.replace('"', '""') + '"' for part in table.split("."))


def _load(root: Path, asset_path: str) -> Callable[..., object]:
    """Import the asset file and return its one asset function.

    The file is loaded by path under a private module name, so a file named
    like an installed package (`assets/gold/polars.py`) shadows nothing.

    Parameters
    ----------
    root
        The unpacked code directory.
    asset_path
        The asset file, relative to `root`.

    Returns
    -------
    callable
        The function carrying an `AssetSpec`.

    Raises
    ------
    AssetLoadError
        If the path leaves `root` or is not a file, the module does not have
        exactly one top-level `def` decorated with `@asset` (the one deploy
        discovery picked; a nested or imported one never counts), that
        function is an ``async def`` (deploy refuses it too), or its name no
        longer refers to that function.
    """
    path = (root / asset_path).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise AssetLoadError(f"{asset_path} is not a file of this repository")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- a .py path always has a loader
        raise AssetLoadError(f"{asset_path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    tree = ast.parse(path.read_bytes())  # bytes: the BOM and CRLF are handled as deploy does
    name_bound, module_bound = _bindings(tree)
    found = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_asset(d, name_bound, module_bound) for d in node.decorator_list)
    ]
    if not found:
        raise AssetLoadError(f"{asset_path} has no function decorated with @asset")
    if len(found) > 1:
        raise AssetLoadError(f"{asset_path} has more than one function decorated with @asset")
    node = found[0]
    if len(node.decorator_list) != 1:
        raise AssetLoadError(
            f"{asset_path}: the asset function must have @asset(...) as its only decorator"
        )
    if isinstance(node, ast.AsyncFunctionDef):
        raise AssetLoadError(f"{asset_path}: an asset function must not be async")
    value = getattr(module, node.name, None)
    if (
        not isinstance(value, types.FunctionType)
        or value.__code__.co_firstlineno != node.decorator_list[0].lineno
        or not isinstance(getattr(value, ASSET_ATTRIBUTE, None), AssetSpec)
    ):
        raise AssetLoadError(
            f"{asset_path}: the asset function {node.name!r} was replaced or wrapped after it "
            "was defined; the name must still refer to the function @asset decorated"
        )
    return value


def _bindings(tree: ast.Module) -> tuple[bool, bool]:
    """Find how the module's top level imports the decorator (as deploy does).

    Parameters
    ----------
    tree
        The parsed module.

    Returns
    -------
    tuple of bool
        Whether ``from lakana import asset`` binds `asset`, and whether
        ``import lakana`` binds `lakana`.
    """
    name_bound = module_bound = False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "lakana" and node.level == 0:
            name_bound = name_bound or any(
                alias.name == "asset" and alias.asname in (None, "asset") for alias in node.names
            )
        elif isinstance(node, ast.Import):
            module_bound = module_bound or any(
                alias.name == "lakana" and alias.asname in (None, "lakana") for alias in node.names
            )
    return name_bound, module_bound


def _is_asset(decorator: ast.expr, name_bound: bool, module_bound: bool) -> bool:
    """Tell whether a decorator is `lakana.asset`, by the rule deploy uses.

    Parameters
    ----------
    decorator
        One decorator expression.
    name_bound
        `_bindings`' first value.
    module_bound
        `_bindings`' second value.

    Returns
    -------
    bool
        `True` for ``@asset(...)`` after ``from lakana import asset`` and
        ``@lakana.asset(...)`` after ``import lakana``.
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id == "asset" and name_bound
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "asset"
        and isinstance(target.value, ast.Name)
        and target.value.id == "lakana"
        and module_bound
    )


def _as_floats(dtype: _DataType) -> _DataType:
    """Replace every decimal in a column type by ``Float64``, however deeply it is nested.

    Parameters
    ----------
    dtype
        A Polars data type, a class such as ``pl.Int64`` or an instance.

    Returns
    -------
    polars.DataType
        `dtype` itself when it holds no decimal (so the caller can tell nothing
        changes); otherwise a copy of the same shape with ``Float64`` where a
        decimal was: lists, fixed-size arrays, structs and, in Polars 2, maps.
    """
    import polars as pl  # lazy: the SDK works without the assets extra

    if dtype.is_decimal():
        return pl.Float64
    if isinstance(dtype, pl.List):
        inner = _as_floats(dtype.inner)
        return dtype if inner is dtype.inner else pl.List(inner)
    if isinstance(dtype, pl.Array):
        inner = _as_floats(dtype.inner)
        return dtype if inner is dtype.inner else pl.Array(inner, dtype.size)
    if isinstance(dtype, pl.Struct):
        old = dtype.fields
        new = [_as_floats(field.dtype) for field in old]
        if all(a is field.dtype for a, field in zip(new, old, strict=True)):
            return dtype
        return pl.Struct([pl.Field(field.name, a) for a, field in zip(new, old, strict=True)])
    map_type = getattr(pl, "Map", None)  # Polars 2 only; in 1.x a map is a list of structs
    if map_type is not None and isinstance(dtype, map_type):
        old_key, old_value = cast(Any, dtype).key, cast(Any, dtype).value
        key, value = _as_floats(old_key), _as_floats(old_value)
        return dtype if key is old_key and value is old_value else map_type(key, value)
    return dtype


def _read_input(
    client: Client, description: TaskDescription, parameter: str, table: str, decimals: str
) -> pl.DataFrame:
    """Read one declared input through the query API.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    parameter
        The function parameter the input is for.
    table
        The dotted table name.
    decimals
        ``float``: decimal columns are cast to ``Float64``; ``exact``: left as they are.

    Returns
    -------
    pl.DataFrame
        The whole table, as the run principal may see it.

    Raises
    ------
    InputError
        If the query is refused, fails or times out.
    """
    import polars as pl  # lazy: the SDK works without the assets extra

    try:
        sql = f"SELECT * FROM {_quoted(table)}"  # nosec B608 -- every component is quoted
        run = client.queries.submit(sql, description.environment)
        run.wait(timeout=description.request_timeout_seconds)
        arrow = run.result()
    except (LakanaError, TimeoutError) as exc:
        reason = str(exc) or type(exc).__name__
        cause = exc.cause if isinstance(exc, QueryFailed) else None
        hint = {"result_too_large": _INPUT_HINT, "out_of_memory": _MEMORY_HINT}.get(cause or "", "")
        raise InputError(f"reading input {parameter!r} ({table}) failed: {reason}{hint}") from exc
    frame = pl.from_arrow(arrow)
    assert isinstance(frame, pl.DataFrame)  # nosec B101 -- a Table always converts to a DataFrame
    if decimals == "exact":
        return frame
    floats: dict[str, Any] = {
        name: new for name, dtype in frame.schema.items() if (new := _as_floats(dtype)) is not dtype
    }
    return frame.cast(cast(Any, floats)) if floats else frame


@dataclass(frozen=True)
class _Output:
    """The output directory of an artifact asset, pinned by a descriptor.

    Parameters
    ----------
    path
        `lakana.output_dir()`: the real path of ``<work_dir>/output``.
    descriptor
        The directory opened without following a link right after it was made,
        before any repository code ran. Everything the harness checks and
        stores is reached from it, never by the path again.
    identity
        The device and inode of that directory. Code of the asset may close the
        descriptor, and the number then goes to the next file opened in the
        process, so the harness checks, before it uses or closes the number, that it
        refers to a directory with this device and inode (`_is_held`). That proves
        the number is not a file or another directory that still exists; it does not
        prove it is the same directory object in time: after a delete and re-create
        on a file system that reuses inode numbers, a new directory can match. Such
        a directory is always one the task made itself inside its own attempt.
    """

    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class _Workspace:
    """The directories the harness made for this attempt.

    Parameters
    ----------
    inputs
        ``<work_dir>/inputs`` when an input is an artifact.
    inputs_identity
        Its device and inode when it was made; a descriptor held on it for the
        attempt keeps the inode from being reused.
    output
        The output directory of an artifact asset.
    """

    inputs: Path | None = None
    inputs_identity: tuple[int, int] | None = None
    output: _Output | None = None


def _is_held(descriptor: int, identity: tuple[int, int]) -> bool:
    """Say whether a descriptor refers to a directory with the identity the harness recorded.

    This is a check of device and inode, not of history: a directory the task deleted and
    re-created can have the same pair on a file system that reuses inode numbers.

    A descriptor number the asset's code closed goes to the next file the process
    opens, so the number alone says nothing about what it names now.

    Parameters
    ----------
    descriptor
        The number the harness holds.
    identity
        The device and inode of the directory it opened under that number.

    Returns
    -------
    bool
        ``True`` if the number is open and names a directory with that identity;
        ``False`` if it is closed or names anything else.
    """
    try:
        info = os.fstat(descriptor)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity


def _identity_of(descriptor: int, held: list[tuple[int, tuple[int, int]]]) -> tuple[int, int]:
    """Read the identity of a directory the harness just opened and put it on the list.

    If reading it fails, the descriptor is closed here, so it cannot leak: the clean-up of
    `_workspace` only knows what is on `held`.

    Parameters
    ----------
    descriptor
        The directory just opened.
    held
        The descriptors the clean-up closes, with their identities; appended to.

    Returns
    -------
    tuple[int, int]
        Its device and inode.
    """
    try:
        info = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    identity = (info.st_dev, info.st_ino)
    held.append((descriptor, identity))
    return identity


def _make_directory(path: Path) -> None:
    """Make a private directory that must not exist yet.

    Parameters
    ----------
    path
        Where.

    Raises
    ------
    OSError
        If it exists or cannot be made; the text shows the path escaped and bounded.
    """
    try:
        os.mkdir(path, 0o700)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise OSError(exc.errno, f"could not create {shown(str(path))}: {reason}") from exc


@contextlib.contextmanager
def _workspace(description: TaskDescription) -> Generator[_Workspace]:
    """Make the attempt's directories before any repository code runs.

    Parameters
    ----------
    description
        The task description.

    Yields
    ------
    _Workspace
        What was made. On exit `lakana.output_dir()` is unset and the descriptors
        of the output and the inputs directory are closed while they still are the
        harness's (`_is_held`), whatever happened.

    Raises
    ------
    WorkspaceError
        For an asset that reads or makes an artifact, on a platform that cannot open files
        relative to a directory without following links; nothing is made before this.
    OSError
        If a directory cannot be made (``inputs`` or ``output`` exists already,
        or the disk is full).
    """
    held: list[tuple[int, tuple[int, int]]] = []
    try:
        inputs = identity = output = None
        work = Path(os.path.realpath(description.work_dir))
        if not _BY_DESCRIPTOR and (
            "artifact" in description.input_kinds.values() or description.output == "artifact"
        ):
            raise WorkspaceError(
                "an artifact asset needs a platform that opens files relative to a "
                "directory without following links (Linux or macOS); run the worker there"
            )
        if "artifact" in description.input_kinds.values():
            inputs = work / "inputs"
            _make_directory(inputs)
            # Held open until the attempt ends: while it is, the file system cannot give
            # this directory's inode to another one, so a directory made in its place
            # after an rmdir never has the same identity.
            pinned = os.open(inputs, _DIR_FLAGS)
            identity = _identity_of(pinned, held)
        if description.output == "artifact":
            path = work / "output"
            _make_directory(path)
            descriptor = os.open(path, _DIR_FLAGS)
            output = _Output(path, descriptor, _identity_of(descriptor, held))
            assets._set_output_dir(path)
        yield _Workspace(inputs, identity, output)
    finally:
        assets._set_output_dir(None)
        # Code of the asset may have closed these (a library calling `os.closerange`), and the
        # number may since name another file of the process (the result record's, say): close
        # a number only while it refers to a directory with the identity recorded here. A
        # failing close must not replace the attempt's result, nor skip the other close.
        for number, expected in held:
            if _is_held(number, expected):
                with contextlib.suppress(OSError):
                    os.close(number)


def _fetch_artifact(
    client: Client,
    description: TaskDescription,
    workspace: _Workspace,
    parameter: str,
    name: str,
) -> Path:
    """Download one artifact input into the attempt's directory.

    The copy is placed by one rename into a target that does not exist yet, so
    its root is sealed read-only too. Before the download the ``inputs``
    directory is checked, by path, to be the one the harness made. That is a
    check, not a guarantee: the asset's module is already imported, so code of
    the task that swaps the directory between this check and the download is
    not stopped. It gives the code nothing it could not do itself; closing it
    needs the download to work by directory descriptor, which is issue #67.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    workspace
        The attempt's directories.
    parameter
        The function parameter the input is for (checked as a plain
        identifier by `parse_description`).
    name
        The artifact's dotted name.

    Returns
    -------
    Path
        The read-only local copy: a file, or a directory.

    Raises
    ------
    WorkspaceError
        If the ``inputs`` directory is, by a check of its path, no longer the one
        the harness made.
    ArtifactInputError
        If the download is refused, fails or does not match its manifest (an
        `InputError`).
    """
    version = description.input_versions[parameter]
    inputs = workspace.inputs
    if inputs is None or not _is_directory(inputs, workspace.inputs_identity):
        raise WorkspaceError(
            "the inputs directory of this attempt was replaced while the task ran, so nothing "
            "was downloaded into it; make sure the asset's code (including its threads and "
            "processes) does not change the attempt's directory, then run the task again"
        )
    try:
        got = client.artifacts.download(
            description.environment, name, inputs / parameter, version=version, read_only=True
        )
    except (LakanaError, OSError) as exc:
        reason = str(exc) or type(exc).__name__
        raise ArtifactInputError(
            f"reading input {parameter!r} ({name}, version {version}) failed: {reason}"
        ) from exc
    return Path(got.path)


def _is_directory(path: Path, identity: tuple[int, int] | None) -> bool:
    """Tell whether a path is still the directory that was made.

    Parameters
    ----------
    path
        The directory's path.
    identity
        Its device and inode when it was made.

    Returns
    -------
    bool
        `True` if it is a directory (not a link) with that identity.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity


def _sources(description: TaskDescription) -> dict[str, int | None]:
    """Name what the task read, for lineage.

    Parameters
    ----------
    description
        The task description.

    Returns
    -------
    dict
        Each input's dotted name to the artifact version the worker resolved
        for it, `None` for a table (the harness does not know its snapshot).
        The version is the description's, never one the function supplies.
    """
    return {
        name: description.input_versions.get(parameter)
        for parameter, name in description.inputs.items()
    }


def _to_arrow(result: object, *, columns_optional: bool = False) -> pa.Table:
    """Validate what the function returned and convert it for the upload.

    Parameters
    ----------
    result
        The function's return value.
    columns_optional
        Accept a result without columns (an append or a merge: its source may have had
        nothing new); the caller decides what such a result means.

    Returns
    -------
    pa.Table
        The rows, with types the platform accepts.

    Raises
    ------
    InvalidResult
        Unless `result` is a `polars.DataFrame` or a `pyarrow.Table` with at least one column
        (unless `columns_optional`) and only column types a table can store.
    """
    if isinstance(result, pa.RecordBatchReader):
        raise InvalidResult(_READER)
    if isinstance(result, pa.Table):
        if result.num_columns == 0 and not columns_optional:
            raise InvalidResult("the asset returned a table without columns")
        return _storable(result)
    return _storable(_polars_to_arrow(result, columns_optional=columns_optional))


def _polars_to_arrow(result: object, *, columns_optional: bool) -> pa.Table:
    """Check a returned polars DataFrame's column types and convert it to Arrow.

    Parameters
    ----------
    result
        The function's return value, which is not a `pyarrow.Table`.
    columns_optional
        Accept a DataFrame without columns.

    Returns
    -------
    pa.Table
        The rows as Arrow, not yet converted to types the platform accepts.

    Raises
    ------
    InvalidResult
        Unless `result` is a `polars.DataFrame` whose column types a table can store.
    """
    import polars as pl  # lazy: the SDK works without the assets extra

    if isinstance(result, pl.LazyFrame):
        raise InvalidResult(
            "the asset returned a polars LazyFrame; call .collect() and return the DataFrame"
        )
    if not isinstance(result, pl.DataFrame):
        kind = type(result)
        module_name = str(getattr(kind, "__module__", None))
        if module_name.split(".")[0] == "pandas" and kind.__name__ == "DataFrame":
            raise InvalidResult("the asset returned a pandas DataFrame; return pl.from_pandas(df)")
        raise InvalidResult(
            "the asset must return a polars DataFrame or a pyarrow Table, got "
            f"{module_name}.{kind.__qualname__}"
        )
    if result.width == 0 and not columns_optional:
        raise InvalidResult("the asset returned a DataFrame without columns")
    extension = getattr(pl, "Extension", None)  # Polars 2 only
    for name, dtype in result.schema.items():
        if dtype in (pl.Object, pl.Unknown):
            raise InvalidResult(
                f"column {name!r} has type {dtype}, which cannot be stored in a table"
            )
        label = shown(name)
        if str(dtype) in ("Int128", "UInt128"):
            # `to_arrow()` would fail with a raw `ArrowInvalid: ... '_pli128'`.
            raise InvalidResult(
                f"column '{label}' has type {dtype}, which cannot be stored in a table; cast it "
                f"to Int64 (pl.col('{label}').cast(pl.Int64)), or to a decimal such as "
                "pl.Decimal(38, 0) if the values do not fit into 64 bits"
            )
        if extension is not None and isinstance(dtype, extension):
            raise InvalidResult(
                f"column '{label}' has type Extension, which cannot be stored in a table; take "
                f"its stored values with pl.col('{label}').ext.storage() and cast them to a "
                "type a table stores"
            )
    return pa.table(result)


def _storable(table: pa.Table) -> pa.Table:
    """Convert view and nanosecond columns, then check every column type.

    Parameters
    ----------
    table
        The rows.

    Returns
    -------
    pa.Table
        The rows, with types the platform accepts.

    Raises
    ------
    InvalidResult
        If a column has a type a table cannot store, or nanoseconds a microsecond timestamp
        would lose.
    """
    try:
        converted = normalize_view_columns(cast_timestamp_ns_columns_to_us(table))
        schema_to_columns(converted.schema)
    except ValidationError as exc:
        raise InvalidResult(str(exc)) from exc
    return converted


def _check_watermark_column(table: pa.Table, column: str) -> None:
    """Require the watermark column in the result, with a type a watermark can have.

    Parameters
    ----------
    table
        The converted result.
    column
        The asset's `watermark` column.

    Raises
    ------
    InvalidResult
        If the column is missing or its type is not one of `WATERMARK_TAGS`.
    """
    if column not in table.column_names:
        raise InvalidResult(
            f"watermark column `{shown(column)}` is not a column of what the asset returned; "
            "return it, or change the asset's watermark="
        )
    (described,) = schema_to_columns(table.select([column]).schema)
    kind = str(described["type"])
    if kind.split("(")[0] not in WATERMARK_TAGS:
        raise InvalidResult(
            f"watermark column `{shown(column)}` is a {kind}; it must be one of "
            f"{', '.join(WATERMARK_TAGS)}"
        )


def _check_merge_rows(table: pa.Table, key: tuple[str, ...]) -> None:
    """Refuse a merge the server would refuse, with advice a Python asset can follow.

    The server checks the same and stays the authority; it words its refusals for a caller
    that sends batches. Key columns the result does not have, null keys (the server counts them
    before it looks for repeats) and nested key types are left to the server.

    Parameters
    ----------
    table
        The converted result.
    key
        The merge key's columns.

    Raises
    ------
    InvalidResult
        For a key of several columns with more than `COMPOSITE_KEY_MAX_ROWS` rows, or a key
        that repeats.
    """
    if len(key) > 1 and table.num_rows > COMPOSITE_KEY_MAX_ROWS:
        raise InvalidResult(
            f"a merge on a key of several columns takes at most {COMPOSITE_KEY_MAX_ROWS:,} rows "
            f"per run, and the asset returned {table.num_rows:,}. Merge on one key column (a "
            "column that holds the whole key), or return fewer rows per run: give the asset a "
            "watermark= and return only the rows past ctx.watermark(), and load the history in "
            f"parts of at most {COMPOSITE_KEY_MAX_ROWS:,} rows"
        )
    if not set(key) <= set(table.column_names) or table.num_rows < 2:
        return
    if any(table.column(column).null_count for column in key):
        return  # the server names null keys first, with their count
    try:
        groups = table.select(list(key)).group_by(list(key)).aggregate([([], "count_all")])
    except (pa.ArrowNotImplementedError, pa.ArrowInvalid, pa.ArrowTypeError):
        return  # a key type the server refuses with its own words
    repeated = sum(count for count in groups.column("count_all").to_pylist() if count > 1)
    if repeated:
        columns = ", ".join(f"`{shown(column)}`" for column in key)
        subset = ", ".join(repr(column) for column in key)
        raise InvalidResult(
            f"key {columns} repeats in {repeated} rows of what the asset returned; return one "
            f"row per key, for example with `df.unique(subset=[{subset}], keep='last')`"
        )


def _returned_text(result: object) -> str:
    """Read the text of a returned path once, trusting nothing else about the object.

    The object is the user's: its methods may lie, raise or change their answer.
    It is accepted only if its exact type is a subclass of `pathlib.Path`
    (`__class__` is not asked) and `os.fspath` gives a `str`; this text is then
    the only thing the harness looks at.

    Parameters
    ----------
    result
        The function's return value.

    Returns
    -------
    str
        The path as written.

    Raises
    ------
    InvalidResult
        If it is not a `pathlib.Path` with a readable text.
    """
    kind = type(result)
    if issubclass(kind, Path):
        try:
            text = os.fspath(cast(Path, result))
        except Exception:
            text = None
        if type(text) is str:
            return text
    name = shown(_safe(lambda: f"{kind.__module__}.{kind.__qualname__}", "an unknown type"))
    hint = "; wrap it in pathlib.Path" if issubclass(kind, str) else ""
    raise InvalidResult(
        "an artifact asset must return a pathlib.Path inside lakana.output_dir() (a file, or "
        f"the directory itself), got {name}{hint}"
    )


_DESCRIPTOR_CLOSED = (
    "the asset's code closed a file descriptor the task needs, so nothing was uploaded; do not "
    "close descriptors you did not open (for example with os.closerange), then run the task again"
)


def _pin(result: object, output: _Output) -> tuple[int, str]:
    """Check what an artifact asset returned and pin the directory it lies in.

    The returned text is judged lexically (``..`` collapsed, so
    ``output/m/../x`` is ``output/x``) against the output directory's real path,
    and then followed from the output directory's descriptor, one component at a
    time, without following a link. What the harness gives the upload is the
    descriptor of the directory to store, or of the directory that holds the one
    file to store, plus a bare name: nothing the user's code can swap by name
    afterwards is looked up again.

    Parameters
    ----------
    result
        The function's return value.
    output
        The attempt's output directory.

    Returns
    -------
    tuple
        An open descriptor the caller must close, and ``"."`` (store that
        directory) or the name of the file in it (store that file).

    Raises
    ------
    InvalidResult
        If it is not a `pathlib.Path` inside the output directory, does not
        exist, passes through a link, was swapped while it was followed, or the
        asset's code closed the harness's descriptor of the output directory.
    """
    text = _returned_text(result)
    base = os.fspath(output.path)
    normal = os.path.normpath(text) if os.path.isabs(text) else ""
    if normal == base:
        parts: list[str] = []
    elif normal.startswith(base + os.sep):
        parts = normal[len(base) + 1 :].split(os.sep)
    else:
        raise InvalidResult(
            f"the asset returned {shown(text)}, which is not inside lakana.output_dir(); "
            "write the artifact's files there and return that directory or a file in it"
        )
    missing = InvalidResult(
        f"the asset returned {shown(text)}, which does not exist; write the file before "
        "returning its path"
    )
    try:
        current = os.dup(output.descriptor)
    except OSError as exc:
        raise InvalidResult(_DESCRIPTOR_CLOSED) from exc
    # The copy is judged, not the number: it is one open file from here on, so a later close
    # or reopen of the number by another thread of the asset's code does not change what is
    # checked and used. A closed number reused for any other file is refused.
    if not _is_held(current, output.identity):
        os.close(current)
        raise InvalidResult(_DESCRIPTOR_CLOSED)
    try:
        for index, part in enumerate(parts):
            try:
                info = os.stat(part, dir_fd=current, follow_symlinks=False)
            except (OSError, ValueError) as exc:  # ValueError: a NUL or a lone surrogate
                if isinstance(exc, OSError) and exc.errno == errno.EACCES:
                    _refuse_unopenable(text, exc)
                raise missing from exc
            if stat.S_ISLNK(info.st_mode):
                raise InvalidResult(
                    f"the asset returned {shown(text)}, which passes through a symbolic link; "
                    "an artifact holds only regular files and directories"
                )
            is_directory = stat.S_ISDIR(info.st_mode)
            if index == len(parts) - 1 and not is_directory:
                return current, part
            if not is_directory:
                raise missing
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    _refuse_unopenable(text, exc)
                raise InvalidResult(
                    f"the asset returned {shown(text)}, which was replaced by a symbolic link or "
                    "another file while it was checked; make sure no thread or process of the "
                    "asset's code changes the output directory, then run the task again"
                ) from exc
            os.close(current)
            current = child
        return current, "."
    except BaseException:
        os.close(current)
        raise


def _refuse_unopenable(text: str, exc: OSError) -> NoReturn:
    """Refuse a returned path whose directory cannot be used.

    Parameters
    ----------
    text
        The returned path as the function gave it.
    exc
        The error the operating system raised.

    Raises
    ------
    InvalidResult
        Always; its text names the cause and the next step.
    """
    reason = exc.strerror or type(exc).__name__
    raise InvalidResult(
        f"the asset returned {shown(text)}, which cannot be opened ({reason}); "
        "keep the permissions of the directories below lakana.output_dir() as "
        "they were made, then run the task again"
    ) from exc


def _upload_pinned(
    client: Client, description: TaskDescription, directory: int, entry: str
) -> ArtifactVersion:
    """Upload ``entry`` of a pinned directory, working inside it.

    The upload scans and re-opens what it stores by name relative to the current
    directory, which is made the pinned directory for the call and restored
    after it. A directory the task's code swaps for a link by name meanwhile is
    therefore not followed by name. This is not a guarantee against code of the
    task that runs at the same time: the working directory belongs to the whole
    process, so a thread of the asset's code that calls ``os.chdir`` during the
    upload redirects the scan. That gives the code nothing it could not do itself
    (it runs with the task's own rights); scanning, uploading and downloading by
    directory descriptor would close it and is issue #67. If the saved directory
    cannot be restored afterwards, the process stays where it is and the upload's
    own outcome is what is recorded: the result record's path is absolute.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    directory
        An open descriptor of the directory; closed here.
    entry
        ``"."`` or the name of a file in the directory.

    Returns
    -------
    ArtifactVersion
        The stored version.

    Raises
    ------
    InvalidResult
        If the directory cannot be entered, or the directory the process was in
        cannot be reopened to be restored (the code changed their permissions or
        its working directory); another attempt would meet the same.
    """
    try:
        try:
            saved = os.open(".", _SAVE_FLAGS)
        except OSError as exc:
            raise InvalidResult(
                "the asset's code left the working directory in a place the harness cannot "
                "reopen (for example a directory without read permission), so nothing was "
                "uploaded; do not change the working directory in the asset, or change it back "
                "before the function returns, then run the task again"
            ) from exc
        try:
            try:
                os.fchdir(directory)
            except OSError as exc:
                raise InvalidResult(
                    "the directory the asset returned (or lakana.output_dir() itself) is one the "
                    "harness cannot enter, so nothing was uploaded; do not remove the "
                    "permissions of the output directory in the asset's code, then run the "
                    "task again"
                ) from exc
            try:
                return client.artifacts.upload(
                    description.environment,
                    description.table,
                    Path(entry),
                    sources=_sources(description),
                )
            finally:
                with contextlib.suppress(OSError):
                    os.fchdir(saved)
        finally:
            os.close(saved)
    finally:
        os.close(directory)


def _store_artifact(
    client: Client, description: TaskDescription, output: _Output, result: object
) -> dict[str, object]:
    """Validate an artifact asset's return value, upload it and build the record.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    output
        The attempt's output directory.
    result
        The function's return value.

    Returns
    -------
    dict
        The ``succeeded`` record of an artifact asset.

    Raises
    ------
    Failure
        Phase ``validate`` for a bad return value (`InvalidResult`); phase
        ``upload`` for a tree the scan refuses (`InvalidResult`, the scan's own
        text), an upload the server refuses (`UploadRefused`) or any other SDK
        error.
    InvalidResult
        Raised for a refused tree and wrapped in a `Failure` of phase ``upload``.
    UploadRefused
        Raised for a refused upload and wrapped in a `Failure` of phase ``upload``.
    """
    with _Phase("validate"):
        directory, entry = _pin(result, output)
    with _Phase("upload"):
        try:
            stored = _upload_pinned(client, description, directory, entry)
        except ManifestError as exc:
            raise InvalidResult(str(exc)) from exc
        except ValidationError as exc:
            raise UploadRefused(str(exc)) from exc
    return {
        "status": "succeeded",
        "output": "artifact",
        "version": stored.version,
        "manifest_sha256": stored.manifest_sha256,
    }


def _upload_message(exc: ValidationError, table: str) -> str:
    """Word a refused upload for the task's error.

    A `table` asset replaces its table with ``evolve_schema`` off, as a SQL
    `table` asset does, so a changed output schema is refused for good. The
    schema mismatch is recognised by the error type the server returns, not by
    its wording, and the harness writes its own advice.

    Parameters
    ----------
    exc
        The refusal.
    table
        The asset's dotted table name.

    Returns
    -------
    str
        The server's reason; for a schema mismatch, the harness's explanation
        with the server's detail and the remedy. The server's own advice to
        pass ``evolve_schema`` is cut off when it is there, because a Python
        asset cannot follow it; a detail worded differently is kept whole.
    """
    if exc.error_type != "SchemaMismatchError":
        return str(exc)
    detail = str(exc).split(_SERVER_ADVICE, 1)[0].strip().rstrip(".")
    return (
        f"the asset's output no longer matches the schema of {table}. Server detail: {detail}. "
        f"Restore the columns and types the table has, or drop {table} and run the asset "
        "again so it is recreated with the new schema. A Python asset cannot pass evolve_schema."
    )


def _import_dependencies() -> None:
    """Import what the harness needs before any repository path is on `sys.path`.

    Everything is imported here, polars and `pyarrow.compute` included, so
    nothing is imported lazily after the repository's own directories exist.

    Raises
    ------
    AssetLoadError
        If polars or another module the harness needs cannot be imported.
    """
    try:
        importlib.import_module("polars")
        importlib.import_module("pyarrow.compute")
    except ImportError as exc:
        if (exc.name or "").split(".")[0] == "polars":
            what = "polars is not installed in the task environment"
        else:
            what = f"the task environment cannot import a module the harness needs ({exc})"
        raise AssetLoadError(
            f"{what}; add lakana[assets] to the project's dependencies and run `uv lock`"
        ) from exc


def run_asset(description: TaskDescription, client: Client, root: Path) -> dict[str, object]:
    """Read the inputs, call the function, validate and upload its result.

    Parameters
    ----------
    description
        The task description.
    client
        The SDK client holding the task's token.
    root
        The unpacked code directory (the working directory).

    Returns
    -------
    dict
        The ``succeeded`` result record.

    Raises
    ------
    Failure
        For anything that goes wrong, including a `BaseException` from the
        user's function; `phase` says where and `cause` holds the original
        (`AssetLoadError`, `InputError`, `InvalidResult`, `UploadRefused`, an
        SDK `Conflict` when the target changed, or the user's own exception).
    InvalidResult
        Raised for a result that cannot be written (a first append or merge without columns)
        and wrapped in a `Failure` of phase ``validate``.
    """
    with contextlib.ExitStack() as stack:
        with _Phase("load"):
            workspace = stack.enter_context(_workspace(description))
            _import_dependencies()
            source = root / "src"
            # Appended: a repository module never shadows the stdlib, the SDK or a package.
            sys.path.extend([str(root), str(source)] if source.is_dir() else [str(root)])
            function = _load(root, description.asset_path)
        decimals = description.decimals or getattr(function, ASSET_ATTRIBUTE).decimals
        values: dict[str, object] = {}
        for parameter, name in description.inputs.items():
            with _Phase("inputs"):
                if description.input_kinds.get(parameter) == "artifact":
                    values[parameter] = _fetch_artifact(
                        client, description, workspace, parameter, name
                    )
                else:
                    values[parameter] = _read_input(client, description, parameter, name, decimals)
        with _Phase("function"):
            if _takes_context(function, description):
                values["ctx"] = AssetContext(description.watermark)
            result = function(**values)
        if workspace.output is not None:
            return _store_artifact(client, description, workspace.output, result)
    with _Phase("validate"):
        replaces = description.write == "replace"
        arrow = _to_arrow(result, columns_optional=not replaces)
        if not replaces and arrow.num_rows == 0 and _table_exists(description):
            # Nothing new since the last run: the table stays as it is. The watermark column is
            # not checked here (an empty result may have no columns at all), so an empty result
            # with a wrongly typed watermark column passes; the next run that has rows fails.
            return {"status": "succeeded", "snapshot_id": None, "rows": 0}
        if arrow.num_columns == 0:
            raise InvalidResult(_NOTHING_TO_CREATE.format(table=description.table))
        if description.watermark_column is not None:
            _check_watermark_column(arrow, description.watermark_column)
        if description.write == "merge":
            _check_merge_rows(arrow, description.key)
    with _Phase("upload"):
        return _upload(client, description, arrow)


def _table_exists(description: TaskDescription) -> bool:
    """Say whether the asset's table existed when the worker read it.

    Parameters
    ----------
    description
        The task description.

    Returns
    -------
    bool
        True when the worker read a snapshot, or said the table exists without one.
    """
    return description.table_exists or description.expected_snapshot_id != "none"


def _takes_context(function: Callable[..., object], description: TaskDescription) -> bool:
    """Say whether the function gets an `AssetContext` as its parameter ``ctx``.

    Parameters
    ----------
    function
        The asset function.
    description
        The task description.

    Returns
    -------
    bool
        Whether it has a parameter ``ctx`` that can be passed by keyword, and the project
        version names no input so: one deployed before ``ctx`` was reserved keeps reading
        that input.
    """
    if "ctx" in description.inputs:
        return False
    try:
        parameter = inspect.signature(function).parameters.get("ctx")
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _upload(client: Client, description: TaskDescription, arrow: pa.Table) -> dict[str, object]:
    """Write the result to the asset's table as its write mode says.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    arrow
        The checked result.

    Returns
    -------
    dict
        The ``succeeded`` record of a table asset; a merge's rows are those it inserted and
        updated.

    Raises
    ------
    UploadRefused
        If the server refuses the rows (`ValidationError`).
    """
    sources = _sources(description)
    lineage: dict[str, Any] = {
        "sources": sorted(sources),
        "source_versions": {n: v for n, v in sources.items() if v is not None},
    }
    expected = description.expected_snapshot_id
    try:
        if description.write == "merge":
            merged = client.tables.merge(
                description.environment,
                description.table,
                arrow,
                key=list(description.key),
                expected_snapshot_id=expected,
                **lineage,
            )
            rows = merged.rows_inserted + merged.rows_updated
            return {"status": "succeeded", "snapshot_id": merged.snapshot_id, "rows": rows}
        if description.write == "append" and _table_exists(description):
            appended = client.tables.append(
                description.environment,
                description.table,
                arrow,
                expected_snapshot_id=expected,
                **lineage,
            )
            return {
                "status": "succeeded",
                "snapshot_id": appended.snapshot_id,
                "rows": appended.rows,
            }
        # A replace, and the first run of an append, which creates the table.
        written = client.tables.replace(
            description.environment,
            description.table,
            arrow,
            evolve_schema=False,
            expected_snapshot_id=expected,
            **lineage,
        )
    except ValidationError as exc:
        raise UploadRefused(_upload_message(exc, description.table)) from exc
    return {"status": "succeeded", "snapshot_id": written.snapshot_id, "rows": written.rows}


_CONTROL_ESCAPES = {0x2028, 0x2029}


def _mask(text: str, token: str) -> str:
    """Replace every occurrence of the task token and of every value registered to hide.

    Every occurrence of every value is a span, overlapping ones included; spans that overlap or
    touch are joined and each joined span becomes one ``***`` (as the worker's redactor does),
    so no part of a value is left beside a mask.

    Parameters
    ----------
    text
        Any text the harness is about to write.
    token
        The task token; empty when the caller removed it already.

    Returns
    -------
    str
        The masked text.
    """
    state = context._state
    hidden = {token, *(state.secrets if state is not None else ())} - {""}
    spans: list[tuple[int, int]] = []
    for value in hidden:
        at = text.find(value)
        while at >= 0:
            spans.append((at, at + len(value)))
            at = text.find(value, at + 1)
    if not spans:
        return text
    spans.sort()
    joined = [spans[0]]
    for start, end in spans[1:]:
        if start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    out, kept = [], 0
    for start, end in joined:
        out += [text[kept:start], "***"]
        kept = end
    out.append(text[kept:])
    return "".join(out)


def _clean(text: str) -> str:
    r"""Make text safe for a result record, a database column and a log line.

    Parameters
    ----------
    text
        Any text, possibly from user code.

    Returns
    -------
    str
        The text with every value registered to hide masked (`_mask`), then control
        characters (except newline and tab), surrogates, format characters (bidi overrides,
        zero-width; not the joiners) and the Unicode line and paragraph separators written as
        ``\xNN`` or ``\uNNNN``.
    """
    out = []
    for char in _mask(text, ""):
        code = ord(char)
        category = unicodedata.category(char)
        if (
            (category == "Cc" and char not in "\n\t")
            or (category == "Cf" and char not in "\u200c\u200d")
            or category == "Cs"
            or code in _CONTROL_ESCAPES
        ):
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def _stderr(text: str) -> None:
    """Write to stderr, ignoring a stream that user code closed or removed.

    Parameters
    ----------
    text
        What to write.
    """
    with contextlib.suppress(Exception):  # reporting must never fail the run
        sys.stderr.write(text)
        sys.stderr.flush()


def _write_record(path: str, record: dict[str, object]) -> bool:
    """Write the result record atomically, never through a link.

    The record goes to a new, unpredictably named file in the same directory
    (`mkstemp` opens with `O_CREAT | O_EXCL`, so an existing link is never
    followed) and is renamed over `path`, which replaces a link there rather
    than writing through it.

    Parameters
    ----------
    path
        `TaskDescription.result_path`.
    record
        The record.

    Returns
    -------
    bool
        `False` (after a message on stderr) if the file could not be written.
    """
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".result-", suffix=".tmp"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        _stderr(f"could not write the result record: {exc.strerror or type(exc).__name__}\n")
        return False
    return True


def _safe(read: Callable[[], str], default: str) -> str:
    """Read a text from user-controlled objects, which may raise anything.

    Parameters
    ----------
    read
        Produces the text.
    default
        Used when `read` raises.

    Returns
    -------
    str
        The text, or `default`.
    """
    try:
        return read()
    except BaseException:
        return default


def _failure_record(exc: BaseException, token: str) -> dict[str, object]:
    """Build the ``failed`` record for whatever ended the run.

    Parameters
    ----------
    exc
        A `Failure`, or anything raised outside the phases.
    token
        The task token, removed from every text with every value registered to hide
        (`_mask`).

    Returns
    -------
    dict
        The record; built field by field, so an exception whose ``str`` or
        class name raises still yields one.
    """
    phase, cause = (exc.phase, exc.cause) if isinstance(exc, Failure) else ("load", exc)
    name = _safe(lambda: type(cause).__name__, "Exception")
    text = _safe(lambda: str(cause), "")
    if text and phase in _USER_CODE_PHASES and not isinstance(cause, AssetLoadError):
        # `KeyError('score')` reads "'score'": an exception of the user's code needs its type.
        text = f"{name}: {text}"
    if isinstance(cause, MemoryError):
        text = f"{text or name}.{_TASK_MEMORY_HINT}"
    message = _clean(_mask(text or name, token))
    if phase == "function" and isinstance(cause, TypeError):
        # The advice must survive the bound on the message: cut the exception's text instead.
        room = MAX_ERROR_MESSAGE_CHARS - len(_TYPE_ERROR_HINT)
        if len(message) > room:
            message = message[: room - 1] + "…"
        message += _TYPE_ERROR_HINT
    return {
        "status": "failed",
        "error_type": _clean(_mask(name, token)),
        "message": message[:MAX_ERROR_MESSAGE_CHARS],
        "retryable": _retryable(phase, cause),
        "phase": phase,
        "conflict": phase == "upload" and isinstance(cause, Conflict),
    }


def main(stdin: TextIO | None = None, client_factory: Callable[..., Client] | None = None) -> int:
    """Run one asset as described on stdin.

    Parameters
    ----------
    stdin
        Where to read the task description; `sys.stdin` when `None`.
    client_factory
        Builds the SDK client from ``(url, token, timeout=...)``; `Client`
        when `None`.

    Returns
    -------
    int
        0 after a ``succeeded`` record, 1 after a failure (with a ``failed``
        record when it could be written), 2 when the description is unreadable.
    """
    try:
        description = parse_description((stdin or sys.stdin).read())
    except ValueError as exc:
        _stderr(f"{exc}\n")
        return 2
    if description.parent_pid is not None and os.getppid() != description.parent_pid:
        _stderr("the worker that started this task is gone\n")
        return 1
    try:
        return _run_described(description, client_factory)
    finally:
        # Only now: the record and the trace are written, so what they hide was known then.
        context._set(None)
        _connections.clear()


def _run_described(
    description: TaskDescription, client_factory: Callable[..., Client] | None
) -> int:
    """Run the described asset and write its result record.

    Parameters
    ----------
    description
        The parsed task description.
    client_factory
        Builds the SDK client; `Client` when `None`.

    Returns
    -------
    int
        0 after a ``succeeded`` record, 1 after a failure.
    """
    token = description.token
    try:
        client = (client_factory or Client)(
            description.server_url, token, timeout=description.request_timeout_seconds
        )
        context._set(
            context._TaskState(
                client=client,
                environment=description.environment,
                asset=description.table,
                declared=frozenset(description.connections),
                connections=description.connections,
            )
        )
        try:
            record = run_asset(description, client, Path.cwd())
        finally:
            with contextlib.suppress(Exception):  # must not mask the outcome
                client.close()
    except BaseException as exc:
        _write_record(description.result_path, _failure_record(exc, token))
        shown = exc.cause if isinstance(exc, Failure) else exc
        try:
            trace = "".join(traceback.format_exception(shown))
        except Exception:
            trace = _clean(_safe(lambda: type(shown).__name__, "Exception")) + "\n"
        _stderr(_mask(trace, token))
        return 1
    return 0 if _write_record(description.result_path, record) else 1


_PR_SET_PDEATHSIG = 1


def die_with_parent() -> None:
    """Ask the kernel to kill this process when the worker that started it dies (Linux).

    Without it a harness whose worker crashed would keep running until its
    token dies at the task's reclaim. Other platforms rely on exactly that.
    The signal fires when the *thread* that started this process exits, so
    the worker keeps that thread alive until the process is reaped. A worker that
    died before this call is caught by the `parent_pid` check in `main`.

    Raises
    ------
    SystemExit
        With status 1 if the kernel refuses the request.
    """
    if not sys.platform.startswith("linux"):
        return
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        _stderr("could not ask the kernel to end this process with its worker\n")
        raise SystemExit(1)


def run_cli() -> NoReturn:
    """Run `main` and end the process at once.

    A non-daemon thread the user's code left running would otherwise keep the
    process alive after the record is written and the table replaced, and the
    worker would report a timeout. The record and the streams are flushed
    first.
    """
    code = 1
    try:
        code = main()
    finally:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):  # user code may have closed them
                stream.flush()
        os._exit(code)


if __name__ == "__main__":  # pragma: no cover -- exercised by `python -m lakana.runner`
    die_with_parent()
    run_cli()
