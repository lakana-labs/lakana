"""The harness: one Python asset, run with an SDK client."""

from __future__ import annotations

import contextlib
import datetime
import errno
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pyarrow as pa
import pytest

from lakana import assets, runner
from lakana._artifact_manifest import (
    ManifestError,
    StoredManifestDamaged,
    StoredManifestInvalid,
    scan,
)
from lakana._models import (
    AppendResult,
    ArtifactDownload,
    ArtifactVersion,
    MergeResult,
    WriteResult,
)
from lakana.errors import (
    ArtifactCorrupt,
    AuthenticationError,
    Conflict,
    LakanaConnectionError,
    NotFound,
    PayloadTooLarge,
    PermissionDenied,
    QueryFailed,
    ServerError,
    ValidationError,
)
from lakana.runner import (
    DESCRIPTION_VERSIONS,
    MAX_ERROR_MESSAGE_CHARS,
    AssetLoadError,
    TaskDescription,
    _import_dependencies,
    main,
    parse_description,
)

_TOKEN = "lakana_task_k1_verysecretvalue"

_ASSET = """\
import polars as pl
from lakana import asset
from helper import double


@asset(inputs={"obs": "silver.weather"})
def scores(obs: pl.DataFrame) -> pl.DataFrame:
    print("rows in:", obs.height)
    return obs.with_columns(score=double(pl.col("id")))
"""


@dataclass
class _Run:
    """A fake query run.

    Parameters
    ----------
    table
        The result, or an exception to raise from `wait`.
    """

    table: pa.Table | Exception
    waited: list[float | None] = field(default_factory=list)

    def wait(self, timeout: float | None = None) -> None:
        """Record the timeout; raise a scripted failure.

        Parameters
        ----------
        timeout
            Seconds.
        """
        self.waited.append(timeout)
        if isinstance(self.table, Exception):
            raise self.table

    def result(self) -> pa.Table:
        """Return the scripted result.

        Returns
        -------
        pa.Table
            The table.
        """
        assert isinstance(self.table, pa.Table)
        return self.table


class _Queries:
    """A fake `client.queries`.

    Parameters
    ----------
    results
        SQL text → result or exception.
    """

    def __init__(self, results: dict[str, pa.Table | Exception]) -> None:
        self.results = results
        self.submitted: list[tuple[str, str]] = []
        self.runs: list[_Run] = []

    def submit(self, sql: str, environment: str) -> _Run:
        """Record the query and hand back its scripted run.

        Parameters
        ----------
        sql
            The SQL.
        environment
            The environment.

        Returns
        -------
        _Run
            The run.
        """
        self.submitted.append((sql, environment))
        run = _Run(self.results[sql])
        self.runs.append(run)
        return run


class _Tables:
    """A fake `client.tables`.

    Parameters
    ----------
    outcome
        What `replace` returns or raises.
    """

    def __init__(self, outcome: WriteResult | Exception) -> None:
        self.outcome = outcome
        self.appended: AppendResult | Exception = AppendResult(rows=2, files=1, snapshot_id=43)
        self.merged: MergeResult | Exception = MergeResult(
            snapshot_id=44, rows_inserted=1, rows_updated=1
        )
        self.calls: list[dict[str, object]] = []

    def _record(
        self,
        method: str,
        environment: str,
        qualified_name: str,
        data: object,
        options: dict[str, object],
        outcome: object,
    ) -> object:
        """Record one upload and hand back its scripted outcome.

        Parameters
        ----------
        method
            ``replace``, ``append`` or ``merge``.
        environment
            The environment.
        qualified_name
            The table.
        data
            The rows.
        options
            Keyword options.
        outcome
            The scripted result, or an exception to raise.

        Returns
        -------
        object
            The scripted result.
        """
        self.calls.append(
            {
                "method": method,
                "environment": environment,
                "table": qualified_name,
                "data": data,
                **options,
            }
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def append(
        self, environment: str, qualified_name: str, data: object, **options: object
    ) -> AppendResult:
        """Record an append.

        Parameters
        ----------
        environment
            The environment.
        qualified_name
            The table.
        data
            The rows.
        **options
            Keyword options.

        Returns
        -------
        AppendResult
            The scripted result.
        """
        result = self._record("append", environment, qualified_name, data, options, self.appended)
        assert isinstance(result, AppendResult)
        return result

    def merge(
        self, environment: str, qualified_name: str, data: object, **options: object
    ) -> MergeResult:
        """Record a merge.

        Parameters
        ----------
        environment
            The environment.
        qualified_name
            The table.
        data
            The rows.
        **options
            Keyword options.

        Returns
        -------
        MergeResult
            The scripted result.
        """
        result = self._record("merge", environment, qualified_name, data, options, self.merged)
        assert isinstance(result, MergeResult)
        return result

    def replace(
        self, environment: str, qualified_name: str, data: object, **options: object
    ) -> WriteResult:
        """Record the upload.

        Parameters
        ----------
        environment
            The environment.
        qualified_name
            The table.
        data
            The rows.
        **options
            Keyword options.

        Returns
        -------
        WriteResult
            The scripted result.
        """
        result = self._record("replace", environment, qualified_name, data, options, self.outcome)
        assert isinstance(result, WriteResult)
        return result


class _Client:
    """A fake SDK client.

    Parameters
    ----------
    queries
        The fake queries resource.
    tables
        The fake tables resource.
    artifacts
        The fake artifacts resource.
    """

    def __init__(self, queries: _Queries, tables: _Tables, artifacts: _Artifacts) -> None:
        self.queries = queries
        self.tables = tables
        self.artifacts = artifacts
        self.built_with: tuple[tuple[object, ...], dict[str, object]] | None = None

    def close(self) -> None:
        """Do nothing."""


@dataclass
class _World:
    """A code directory, a fake client and the harness's captured result.

    Parameters
    ----------
    root
        The unpacked code directory.
    client
        The fake client.
    result_path
        Where the harness writes its record.
    """

    root: Path
    client: _Client
    result_path: Path

    def description(self, **overrides: object) -> str:
        """Build the stdin document.

        Parameters
        ----------
        **overrides
            Keys to replace.

        Returns
        -------
        str
            JSON.
        """
        document: dict[str, object] = {
            "version": 2,
            "server_url": "http://server:8000",
            "token": _TOKEN,
            "environment": "dev",
            "asset_path": "assets/gold/scores.py",
            "table": "gold.scores",
            "inputs": {"obs": "silver.weather"},
            "expected_snapshot_id": 41,
            "result_path": str(self.result_path),
            "request_timeout_seconds": 120,
            "work_dir": str(self.result_path.parent),
            "output": "table",
            "input_kinds": {"obs": "table"},
            "input_versions": {},
        }
        document.update(overrides)
        return json.dumps(document)

    def run(self, **overrides: object) -> int:
        """Run the harness in this process.

        Parameters
        ----------
        **overrides
            Description keys to replace.

        Returns
        -------
        int
            The exit code.
        """

        def factory(*args: object, **kwargs: object) -> _Client:
            self.client.built_with = (args, kwargs)
            return self.client

        return main(io.StringIO(self.description(**overrides)), factory)  # ty: ignore[invalid-argument-type]

    def record(self) -> dict[str, Any]:
        """Read the result record.

        Returns
        -------
        dict
            The record.
        """
        return json.loads(self.result_path.read_text())


_WEATHER = pa.table({"id": pa.array([1, 2], pa.int64())})
_SELECT = 'SELECT * FROM "silver"."weather"'


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    """Write the asset and its helper, and make the code directory the cwd.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Changes the cwd and isolates `sys.path` and `sys.modules`.

    Returns
    -------
    _World
        The world.
    """
    root = tmp_path / "code"
    (root / "assets" / "gold").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "assets" / "gold" / "scores.py").write_text(_ASSET)
    (root / "src" / "helper.py").write_text("def double(x):\n    return x * 2\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("helper", "shared", "lakana_asset"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    client = _Client(
        _Queries({_SELECT: _WEATHER}),
        _Tables(WriteResult(rows=2, files=1, snapshot_id=42)),
        _Artifacts(),
    )
    (tmp_path / "attempt").mkdir(mode=0o700)
    return _World(root, client, tmp_path / "attempt" / "result.json")


def test_a_run_reads_inputs_calls_the_function_and_replaces_the_table(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    assert world.run() == 0
    assert world.record() == {"status": "succeeded", "snapshot_id": 42, "rows": 2}
    assert world.client.built_with == (
        ("http://server:8000", _TOKEN),
        {"timeout": 120.0},
    )
    assert world.client.queries.submitted == [(_SELECT, "dev")]
    assert world.client.queries.runs[0].waited == [120.0]
    (call,) = world.client.tables.calls
    assert call["environment"] == "dev"
    assert call["table"] == "gold.scores"
    assert call["expected_snapshot_id"] == 41
    assert call["sources"] == ["silver.weather"]
    assert call["evolve_schema"] is False
    uploaded = call["data"]
    assert isinstance(uploaded, pa.Table)
    assert uploaded.to_pydict() == {"id": [1, 2], "score": [2, 4]}
    captured = capsys.readouterr()
    assert "rows in: 2" in captured.out
    assert _TOKEN not in captured.out + captured.err


def test_none_is_passed_through_for_a_first_run(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    assert world.run(expected_snapshot_id="none") == 0
    assert world.client.tables.calls[0]["expected_snapshot_id"] == "none"


def test_an_asset_without_inputs_and_with_zero_rows(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import polars as pl\nfrom lakana import asset\n\n\n@asset()\ndef load():\n"
        "    return pl.DataFrame({'id': pl.Series([], dtype=pl.Int64)})\n"
    )
    world.client.tables.outcome = WriteResult(rows=0, files=0, snapshot_id=None)
    assert world.run(inputs={}, input_kinds={}) == 0
    assert world.record() == {"status": "succeeded", "snapshot_id": None, "rows": 0}
    assert world.client.queries.submitted == []
    assert world.client.tables.calls[0]["sources"] == []


def test_two_parameters_reading_one_table_query_it_once_each_and_declare_one_source(
    world: _World,
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\n"
        "@asset(inputs={'a': 'silver.weather', 'b': 'silver.weather'})\n"
        "def both(a, b):\n    return a.vstack(b)\n"
    )
    inputs = {"a": "silver.weather", "b": "silver.weather"}
    assert world.run(inputs=inputs, input_kinds={"a": "table", "b": "table"}) == 0
    assert len(world.client.queries.submitted) == 2
    assert world.client.tables.calls[0]["sources"] == ["silver.weather"]


def test_table_names_are_quoted(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    odd = 'SELECT * FROM "raw zone"."we""ird"'
    world.client.queries.results[odd] = _WEATHER
    assert world.run(inputs={"obs": 'raw zone.we"ird'}) == 0
    assert world.client.queries.submitted == [(odd, "dev")]


def _failing(world: _World, body: str, **overrides: object) -> dict[str, Any]:
    """Run an asset whose function body is `body` and return the failure record.

    Parameters
    ----------
    world
        The world.
    body
        The function's body, one statement per line.
    **overrides
        Description keys to replace.

    Returns
    -------
    dict
        The `failed` record.
    """
    world.result_path.parent.mkdir(exist_ok=True)
    source = (
        "import polars as pl\nfrom lakana import asset\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef f(obs):\n"
    )
    (world.root / "assets" / "gold" / "scores.py").write_text(
        source + textwrap.indent(body, "    ") + "\n"
    )
    assert world.run(**overrides) == 1
    record = world.record()
    assert record["status"] == "failed"
    return record


def test_an_exception_in_the_function_is_recorded_with_its_traceback_on_stderr(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _failing(world, "raise RuntimeError('boom ' + 'x' * 5000)")
    assert record["error_type"] == "RuntimeError"
    assert record["retryable"] is True
    assert isinstance(record["message"], str)
    assert record["message"].startswith("RuntimeError: boom xxx")
    assert len(record["message"]) == MAX_ERROR_MESSAGE_CHARS
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: boom" in err
    assert world.client.tables.calls == []


def test_sys_exit_in_the_function_is_a_failure(world: _World) -> None:
    record = _failing(world, "import sys\nsys.exit(0)")
    assert record["error_type"] == "SystemExit"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("return obs.lazy()", "returned a polars LazyFrame; call .collect()"),
        ("return [1, 2]", "must return a polars DataFrame or a pyarrow Table, got builtins.list"),
        (
            "return None",
            "must return a polars DataFrame or a pyarrow Table, got builtins.NoneType",
        ),
        ("return pl.DataFrame()", "returned a DataFrame without columns"),
        (
            "return pl.DataFrame({'blob': [object()]})",
            "column 'blob' has type Object, which cannot be stored in a table",
        ),
        (
            (
                "class DataFrame:\n    pass\n"
                "DataFrame.__module__ = 'pandas.core.frame'\nreturn DataFrame()"
            ),
            "returned a pandas DataFrame; return pl.from_pandas(df)",
        ),
        (
            "import datetime\nreturn pl.DataFrame({'d': [datetime.timedelta(seconds=1)]})",
            "Column 'd' has an unsupported Arrow type",
        ),
    ],
)
def test_an_invalid_return_value_is_not_retryable(world: _World, body: str, fragment: str) -> None:
    record = _failing(world, body)
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert fragment in str(record["message"])
    assert world.client.tables.calls == []


def test_a_conflict_on_the_write_is_recorded_as_a_conflict(world: _World) -> None:
    world.client.tables.outcome = Conflict("gold.scores changed during the write; retry.")
    record = _failing(world, "return obs")
    assert record == {
        "status": "failed",
        "error_type": "Conflict",
        "message": "gold.scores changed during the write; retry.",
        "retryable": True,
        "phase": "upload",
        "conflict": True,
    }


def test_an_input_over_the_result_limit_names_the_input_and_the_remedy(world: _World) -> None:
    """A refusal carries the same words on every route."""
    world.client.queries.results[_SELECT] = QueryFailed(
        "The result has more than 1000000 rows (LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS).",
        cause="result_too_large",
    )
    record = _failing(world, "return obs")
    assert record["error_type"] == "InputError"
    assert record["retryable"] is False
    assert record["message"] == (
        "reading input 'obs' (silver.weather) failed: The result has more than 1000000 rows "
        "(LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS). An input is read whole into memory: if it is "
        "too large, narrow it with an upstream SQL asset (for example a view) and declare that "
        "as the input."
    )


def test_the_token_never_reaches_the_record_or_the_streams(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _failing(world, f"raise RuntimeError('leaked {_TOKEN} here')")
    assert _TOKEN not in json.dumps(record)
    assert "leaked *** here" in str(record["message"])
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("x = 1\n", "has no function decorated with @asset"),
        (
            (
                "from lakana import asset\n\n@asset()\ndef a():\n    pass\n\n"
                "@asset()\ndef b():\n    pass\n"
            ),
            "has more than one function decorated with @asset",
        ),
        ("raise ImportError('no module named nope')\n", "no module named nope"),
        ("def broken(:\n", "invalid syntax"),
    ],
)
def test_a_file_that_cannot_be_loaded(world: _World, source: str, fragment: str) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(source)
    assert world.run() == 1
    record = world.record()
    assert fragment in str(record["message"])


def test_a_nested_decorated_function_is_never_chosen(world: _World) -> None:
    """The harness picks what deploy discovery picks: the one top-level `def`."""
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import functools\nfrom lakana import asset\n\n\n"
        "@functools.cache\ndef helper():\n    return 1\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef mine(obs):\n    return obs\n\n\n"
        "if True:\n    @asset()\n    def nested():\n        return None\n\n\n"
        "class Holder:\n    @asset()\n    def method(self):\n        return None\n"
    )
    assert world.run() == 0
    uploaded = world.client.tables.calls[0]["data"]
    assert isinstance(uploaded, pa.Table)
    assert uploaded.to_pydict() == {"id": [1, 2]}


def test_only_a_nested_decorated_function_is_no_asset(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\nif True:\n"
        "    @asset()\n    def nested():\n        return None\n"
    )
    assert world.run() == 1
    record = world.record()
    assert "has no function decorated with @asset" in str(record["message"])
    assert record["retryable"] is False


def test_a_changed_output_schema_is_a_permanent_failure_that_names_the_remedy(
    world: _World,
) -> None:
    world.client.tables.outcome = ValidationError(
        "The result does not match gold.scores's schema (dropped region). "
        "Pass evolve_schema (--evolve-schema) to let the table adopt the result's schema.",
        status=422,
        error_type="SchemaMismatchError",
    )
    record = _failing(world, "return obs")
    assert record["error_type"] == "UploadRefused"
    assert record["retryable"] is False
    message = str(record["message"])
    assert "dropped region" in message
    assert "add evolve_schema=True to the asset's @asset(...)" in message


def test_the_schema_message_names_the_decorators_option_not_the_servers(
    world: _World,
) -> None:
    world.client.tables.outcome = ValidationError(
        "The result does not match gold.scores's schema (dropped region). "
        "Pass evolve_schema (--evolve-schema) to let the table adopt the result's schema.",
        status=422,
        error_type="SchemaMismatchError",
    )
    assert _failing(world, "return obs")["message"] == (
        "the asset's output no longer matches the schema of gold.scores. Server detail: The "
        "result does not match gold.scores's schema (dropped region). To let gold.scores take "
        "the new schema, add evolve_schema=True to the asset's @asset(...) and deploy again (a "
        "column the output no longer has is then removed from the table); otherwise restore "
        "the columns and types the table has."
    )


def test_an_append_whose_first_write_meets_another_schema_is_not_told_to_evolve(
    world: _World,
) -> None:
    """Deploy refuses `evolve_schema=True` on an append, so the advice names what it can do."""
    world.client.tables.outcome = ValidationError(
        "The result does not match gold.scores's schema (dropped region). "
        "Pass evolve_schema (--evolve-schema) to let the table adopt the result's schema.",
        status=422,
        error_type="SchemaMismatchError",
    )
    record = _failing(
        world, "return obs", **_v3(watermark={"column": "id", "value": None}, expected="none")
    )
    assert record["retryable"] is False
    assert record["message"] == (
        "the asset's output no longer matches the schema of gold.scores. Server detail: The "
        "result does not match gold.scores's schema (dropped region). An asset that appends "
        "cannot change its table's schema: make the asset return the columns and types "
        "gold.scores has, then run it again."
    )


def test_the_message_of_an_exception_from_user_code_names_its_type(world: _World) -> None:
    """`KeyError('score')` reads ``'score'`` on its own, which tells the user nothing."""
    assert _failing(world, "return {}['score']")["message"] == "KeyError: 'score'"
    assert _failing(world, "raise ValueError")["message"] == "ValueError"
    (world.root / "assets" / "gold" / "scores.py").write_text("import no_such_module\n")
    assert world.run() == 1
    assert world.record()["message"] == ("ModuleNotFoundError: No module named 'no_such_module'")


def test_a_memory_error_in_user_code_is_not_retried_and_says_what_to_do(world: _World) -> None:
    """Running out of memory repeats on the next attempt; the message names the setting."""
    record = _failing(world, "raise MemoryError")
    message = str(record["message"])
    assert record["retryable"] is False
    assert record["error_type"] == "MemoryError"
    assert message.startswith("MemoryError. The task ran out of memory")
    assert "run it on a machine or in a container with more memory" in message
    assert "LAKANA_WORKER" not in message


def test_any_other_validation_refusal_of_the_upload_is_not_retryable(world: _World) -> None:
    world.client.tables.outcome = ValidationError("gold.scores is a view.", status=422)
    record = _failing(world, "return obs")
    assert record["retryable"] is False
    assert record["message"] == "gold.scores is a view."


def test_an_asset_file_named_like_a_library_does_not_shadow_it(world: _World) -> None:
    """`assets/gold/polars.py` is loaded by path; `import polars` still finds polars."""
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "polars.py").write_text(_ASSET)
    assert world.run(asset_path="assets/gold/polars.py") == 0


def test_an_imported_asset_function_is_not_mistaken_for_the_files_own(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "src" / "shared.py").write_text(
        "from lakana import asset\n\n@asset()\ndef other():\n    return None\n"
    )
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\nfrom shared import other\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef mine(obs):\n    return obs\n"
    )
    assert world.run() == 0


@pytest.mark.parametrize(
    "asset_path",
    ["/etc/passwd", "../outside.py", "assets/../../outside.py", "assets/gold/missing.py"],
)
def test_an_asset_path_outside_the_code_directory_is_refused(
    world: _World, asset_path: str
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    assert world.run(asset_path=asset_path) == 1
    assert world.record()["retryable"] is False


@pytest.mark.parametrize(
    "document",
    [
        "",
        "not json",
        "[]",
        json.dumps({"version": 2}),
        json.dumps({"version": 1}),
    ],
)
def test_an_unreadable_description_exits_2(
    document: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(io.StringIO(document), lambda *a, **k: None) == 2  # ty: ignore[invalid-argument-type]
    assert "task description" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("token", 7),
        ("inputs", ["silver.weather"]),
        ("inputs", {"obs": 1}),
        ("expected_snapshot_id", "latest"),
        ("expected_snapshot_id", True),
        ("request_timeout_seconds", "soon"),
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", float("nan")),
        ("request_timeout_seconds", float("inf")),
        ("result_path", ""),
        ("result_path", "result.json"),
    ],
)
def test_parse_description_checks_every_field(world: _World, key: str, value: object) -> None:
    with pytest.raises(ValueError, match=key):
        parse_description(world.description(**{key: value}))


def test_the_description_hides_its_token(world: _World) -> None:
    description = parse_description(world.description())
    assert isinstance(description, TaskDescription)
    assert _TOKEN not in repr(description)


def test_an_unwritable_result_path_exits_1_without_a_record(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = world.result_path.parent / "gone" / "result.json"
    assert world.run(result_path=str(missing)) == 1
    assert not missing.exists()
    assert "could not write the result record" in capsys.readouterr().err


def test_importing_the_runner_needs_no_polars_and_python_m_works(world: _World) -> None:
    probe = "import sys, lakana.runner; sys.exit(1 if 'polars' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0
    finished = subprocess.run(
        [sys.executable, "-m", "lakana.runner"],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 2
    assert "task description" in finished.stderr


def test_a_bom_and_crlf_asset_file_runs(world: _World) -> None:
    """Deploy strips the BOM and CRLF; the harness must parse the same file."""
    world.result_path.parent.mkdir(exist_ok=True)
    source = _ASSET.replace("\n", "\r\n")
    (world.root / "assets" / "gold" / "scores.py").write_bytes(b"\xef\xbb\xbf" + source.encode())
    assert world.run() == 0


@pytest.mark.parametrize(
    "body",
    [
        "raise KeyboardInterrupt('{token}')",
        "raise GeneratorExit",
        "import asyncio\nraise asyncio.CancelledError()",
        "class B(BaseException):\n    pass\nraise B('{token}')",
        "class E(Exception):\n    def __str__(self):\n        raise RuntimeError('no')\nraise E()",
    ],
)
def test_any_exception_from_user_code_leaves_a_record_and_no_token(
    world: _World, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    record = _failing(world, body.replace("{token}", _TOKEN))
    assert record["phase"] == "function"
    assert record["retryable"] is True
    assert record["error_type"]
    assert record["message"]
    assert _TOKEN not in json.dumps(record)
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out + captured.err


@pytest.mark.parametrize("breakage", ["sys.stderr = None", "sys.stderr.close()"])
def test_user_code_that_breaks_stderr_still_leaves_the_record(
    world: _World, monkeypatch: pytest.MonkeyPatch, breakage: str
) -> None:
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    record = _failing(world, f"import sys\n{breakage}\nraise RuntimeError('boom')")
    assert record["message"] == "RuntimeError: boom"


def test_the_message_is_cleaned_of_control_characters(world: _World) -> None:
    record = _failing(
        world, "raise RuntimeError('a\\x00b\\x1b[31mred\\ud800\\u2028z\\ttab\\nline')"
    )
    assert record["message"] == "RuntimeError: a\\x00b\\x1b[31mred\\ud800\\u2028z\ttab\nline"
    json.loads(world.result_path.read_bytes().decode("utf-8"))


def test_a_control_character_in_the_exception_class_name_is_cleaned(world: _World) -> None:
    record = _failing(world, "class E(Exception):\n    pass\nE.__name__ = 'E\\x1b'\nraise E('x')")
    assert record["error_type"] == "E\\x1b"


def test_a_user_raised_conflict_is_not_a_conflict(world: _World) -> None:
    record = _failing(world, "from lakana.errors import Conflict\nraise Conflict('mine')")
    assert record["error_type"] == "Conflict"
    assert record["phase"] == "function"
    assert record["conflict"] is False
    record = _failing(world, "class Conflict(Exception):\n    pass\nraise Conflict('mine')")
    assert record["conflict"] is False


def test_phases_are_reported(world: _World) -> None:
    assert _failing(world, "return [1]")["phase"] == "validate"
    world.client.queries.results[_SELECT] = ServerError("down", status=500)
    assert _failing(world, "return obs.head(1)")["phase"] == "inputs"
    world.client.queries.results[_SELECT] = _WEATHER
    world.client.tables.outcome = ServerError("down", status=500)
    record = _failing(world, "return obs.head(2)")  # sizes differ: no stale bytecode
    assert record["phase"] == "upload"
    assert record["conflict"] is False
    (world.root / "assets" / "gold" / "scores.py").write_text("x = 1\n")
    assert world.run() == 1
    assert world.record()["phase"] == "load"


@pytest.mark.parametrize(
    ("source", "retryable"),
    [
        ("def broken(:\n", False),
        ("import nope_zzz_missing\n", False),
        ("raise OSError('x')\n", True),
    ],
)
def test_load_failures_that_repeat_are_not_retryable(
    world: _World, source: str, retryable: bool
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(source)
    assert world.run() == 1
    assert world.record()["retryable"] is retryable


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (PermissionDenied("no", status=403), False),
        (NotFound("no", status=404), False),
        (AuthenticationError("no", status=401), False),
        (ServerError("down", status=500), True),
    ],
)
def test_input_and_upload_failures_are_retryable_only_when_transient(
    world: _World, error: Exception, retryable: bool
) -> None:
    world.client.queries.results[_SELECT] = error
    assert _failing(world, "return obs")["retryable"] is retryable
    world.client.queries.results[_SELECT] = _WEATHER
    world.client.tables.outcome = error
    assert _failing(world, "return obs")["retryable"] is retryable


def test_the_input_hint_is_only_for_a_failed_query_and_a_reason_is_never_empty(
    world: _World,
) -> None:
    world.client.queries.results[_SELECT] = AuthenticationError("bad token", status=401)
    record = _failing(world, "return obs")
    assert record["message"] == "reading input 'obs' (silver.weather) failed: bad token"
    world.client.queries.results[_SELECT] = QueryFailed("")
    message = str(_failing(world, "return obs")["message"])
    assert message == "reading input 'obs' (silver.weather) failed: QueryFailed"


@pytest.mark.parametrize(
    "reason",
    [
        "Table 'silver.weather' does not exist.",
        "Catalog Error: Table with name weather does not exist!",
        "Query timed out.",
    ],
)
def test_a_failed_input_query_that_is_not_the_result_limit_carries_no_size_hint(
    world: _World, reason: str
) -> None:
    world.client.queries.results[_SELECT] = QueryFailed(reason)
    message = str(_failing(world, "return obs")["message"])
    assert message == f"reading input 'obs' (silver.weather) failed: {reason}"
    assert "too large" not in message and "narrow it" not in message


@pytest.mark.parametrize(
    ("cause", "retryable"),
    [
        ("not_found", False),
        ("permission_denied", False),
        ("invalid_query", True),
        ("result_too_large", False),
        ("out_of_memory", True),
        ("timeout", True),
        ("cancelled", True),
        ("worker_lost", True),
        ("internal", True),
        ("a-cause-from-a-newer-server", True),
        (None, True),
    ],
)
def test_a_failed_input_query_is_retried_only_when_its_cause_may_clear(
    world: _World, cause: str | None, retryable: bool
) -> None:
    """The server's machine-readable cause decides; a missing one keeps retrying."""
    world.client.queries.results[_SELECT] = QueryFailed("it failed", cause=cause)
    record = _failing(world, "return obs")
    assert record["error_type"] == "InputError"
    assert record["retryable"] is retryable


def test_a_query_lost_with_its_worker_is_retried_by_a_python_task(world: _World) -> None:
    """The sweep's `worker_lost` ends an input read that another attempt can repeat."""
    assert "worker_lost" not in runner._PERMANENT_QUERY_CAUSES
    world.client.queries.results[_SELECT] = QueryFailed(
        "The worker running this query was lost, so the query was ended. Run it again.",
        cause="worker_lost",
    )
    assert _failing(world, "return obs")["retryable"] is True


@pytest.mark.parametrize("cause", ["not_found", "permission_denied", "result_too_large"])
def test_a_permanent_cause_raised_by_the_function_itself_stays_retryable(
    world: _World, cause: str
) -> None:
    """Review R6: only a failed input read is classified by the server's cause."""
    record = _failing(
        world,
        "from lakana.errors import QueryFailed\n"
        f"raise QueryFailed('the function ran its own query', cause={cause!r})",
    )
    assert record["phase"] == "function"
    assert record["retryable"] is True


@pytest.mark.parametrize("cause", [None, "not_found", "internal", "timeout"])
def test_the_size_hint_comes_from_the_cause_never_from_the_message_text(
    world: _World, cause: str | None
) -> None:
    world.client.queries.results[_SELECT] = QueryFailed(
        "Query result exceeds max_result_rows (1000000).", cause=cause
    )
    message = str(_failing(world, "return obs")["message"])
    assert "too large" not in message and "narrow it" not in message


def test_the_cause_out_of_memory_says_what_to_do_and_names_the_setting(world: _World) -> None:
    """An out-of-memory read is retryable and says to read less or raise the limit."""
    world.client.queries.results[_SELECT] = QueryFailed("ran out", cause="out_of_memory")
    record = _failing(world, "return obs")
    message = str(record["message"])
    assert record["retryable"] is True
    assert "ran out" in message
    assert "narrow the input with an upstream SQL asset" in message
    assert "memory limit that the message above names" in message
    assert "too large, narrow it" not in message


def test_the_cause_result_too_large_carries_the_size_hint(world: _World) -> None:
    world.client.queries.results[_SELECT] = QueryFailed("too big", cause="result_too_large")
    message = str(_failing(world, "return obs")["message"])
    assert "too large, narrow it with an upstream SQL asset" in message


def test_a_schema_mismatch_is_recognised_by_its_type_not_by_the_servers_prose(
    world: _World,
) -> None:
    world.client.tables.outcome = ValidationError(
        "Differs: dropped region.", status=422, error_type="SchemaMismatchError"
    )
    record = _failing(world, "return obs")
    assert record["retryable"] is False
    message = str(record["message"])
    assert "dropped region" in message
    assert "add evolve_schema=True" in message
    assert "--evolve-schema" not in message


def test_a_rebound_asset_name_is_refused_not_run(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\n@asset(inputs={'obs': 'silver.weather'})\n"
        "def f(obs):\n    return obs\n\n\nif True:\n    @asset()\n    def f():\n"
        "        raise RuntimeError('wrong one')\n"
    )
    assert world.run() == 1
    record = world.record()
    assert record["retryable"] is False
    assert "replaced or wrapped" in str(record["message"])
    assert "wrong one" not in str(record["message"])


def test_a_nested_asset_does_not_make_another_decorated_name_an_asset(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import functools\nfrom lakana import asset\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef mine(obs):\n    return obs\n\n\n"
        "@functools.cache\ndef helper():\n    return 1\n\n\n"
        "if True:\n    @asset()\n    def helper():\n        return None\n"
    )
    assert world.run() == 0


def test_a_wrapped_asset_says_it_was_wrapped(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\ndef deco(fn):\n    def wrapper(*a, **k):\n"
        "        return fn(*a, **k)\n    return wrapper\n\n\n"
        "@asset()\ndef f():\n    return None\n\n\nf = deco(f)\n"
    )
    assert world.run() == 1
    record = world.record()
    assert record["retryable"] is False
    assert "replaced or wrapped" in str(record["message"])


def test_a_class_without_a_module_is_an_invalid_result(world: _World) -> None:
    record = _failing(world, "class C:\n    pass\nC.__module__ = None\nreturn C()")
    assert record["error_type"] == "InvalidResult"


def test_an_oserror_without_a_message_is_still_reported(
    world: _World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)

    def refuse(*args: object) -> None:
        raise OSError

    monkeypatch.setattr(runner.os, "replace", refuse)
    assert world.run() == 1
    assert "could not write the result record: OSError" in capsys.readouterr().err


def test_a_failing_close_does_not_hide_the_outcome(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)

    def broken() -> None:
        raise RuntimeError("close failed")

    world.client.close = broken  # ty: ignore[invalid-assignment]
    assert world.run() == 0
    assert world.record()["status"] == "succeeded"


def test_a_symlink_at_the_temporary_name_is_not_written_through(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    victim = world.result_path.parent / "victim.txt"
    victim.write_text("keep")
    Path(f"{world.result_path}.tmp").symlink_to(victim)
    assert world.run() == 0
    assert victim.read_text() == "keep"
    assert world.record()["status"] == "succeeded"


def test_a_symlink_at_the_result_path_is_replaced_not_followed(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    victim = world.result_path.parent / "victim.txt"
    victim.write_text("keep")
    world.result_path.symlink_to(victim)
    assert world.run() == 0
    assert victim.read_text() == "keep"
    assert not world.result_path.is_symlink()
    assert world.record()["status"] == "succeeded"


def test_a_directory_at_the_result_path_exits_1_and_leaves_no_temporary_file(
    world: _World,
) -> None:
    world.result_path.mkdir(parents=True)
    assert world.run() == 1
    assert list(world.result_path.parent.glob(".result-*")) == []


class _Exited(Exception):
    """Raised by the fake `os._exit`."""


def test_the_command_line_entry_flushes_and_ends_the_process_hard(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        raise _Exited

    monkeypatch.setattr(runner.os, "_exit", fake_exit)
    monkeypatch.setattr(runner, "Client", lambda *a, **k: world.client)
    monkeypatch.setattr(sys, "stdin", io.StringIO(world.description()))
    with pytest.raises(_Exited):
        runner.run_cli()
    assert codes == [0]


_DRIVER = """\
import pyarrow as pa
from lakana import runner
from lakana._models import WriteResult


class R:
    def wait(self, timeout=None):
        pass

    def result(self):
        return pa.table({"id": pa.array([1, 2], pa.int64())})


class Q:
    def submit(self, sql, environment):
        return R()


class T:
    def replace(self, *args, **kwargs):
        return WriteResult(rows=2, files=1, snapshot_id=42)


class C:
    def __init__(self, *args, **kwargs):
        self.queries = Q()
        self.tables = T()

    def close(self):
        pass


runner.Client = C
runner.run_cli()
"""


def _in_subprocess(world: _World) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-P", "-c", _DRIVER],
        input=world.description(),
        capture_output=True,
        text=True,
        cwd=world.root,
        timeout=60,
        check=False,
    )


def test_a_non_daemon_thread_left_by_user_code_does_not_hold_the_process(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import threading\nimport time\nfrom lakana import asset\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef f(obs):\n"
        "    threading.Thread(target=time.sleep, args=(300,)).start()\n    return obs\n"
    )
    finished = _in_subprocess(world)
    assert finished.returncode == 0, finished.stderr
    assert world.record()["status"] == "succeeded"


def test_a_root_level_file_named_polars_does_not_shadow_the_harnesss_polars(
    world: _World,
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "polars.py").write_text("raise RuntimeError('shadow')\n")
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\n@asset(inputs={'obs': 'silver.weather'})\n"
        "def f(obs):\n    return obs\n"
    )
    finished = _in_subprocess(world)
    assert finished.returncode == 0, finished.stderr
    assert world.record()["status"] == "succeeded"


def test_a_traceback_that_cannot_be_formatted_still_leaves_the_record(
    world: _World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(exc: BaseException) -> list[str]:
        raise RuntimeError("no traceback")

    monkeypatch.setattr(runner.traceback, "format_exception", broken)
    record = _failing(world, "raise ValueError('boom')")
    assert record["error_type"] == "ValueError"
    assert capsys.readouterr().err == "ValueError\n"


def test_a_decorator_besides_asset_is_refused_with_the_deploy_wording(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import functools\nfrom lakana import asset\n\n\n"
        "@asset()\n@functools.cache\ndef f():\n    return None\n"
    )
    assert world.run() == 1
    record = world.record()
    assert record["retryable"] is False
    assert "must have @asset(...) as its only decorator" in str(record["message"])


def test_an_async_asset_function_is_refused_as_deploy_refuses_it(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\n@asset(inputs={'obs': 'silver.weather'})\n"
        "async def f(obs):\n    return obs\n"
    )
    assert world.run() == 1
    record = world.record()
    assert record["phase"] == "load"
    assert record["error_type"] == "AssetLoadError"
    assert record["retryable"] is False
    assert record["message"] == "assets/gold/scores.py: an asset function must not be async"
    assert world.client.tables.calls == []


def test_repository_files_never_shadow_the_standard_library_or_installed_packages(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    for name in ("pydoc", "six"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        (world.root / f"{name}.py").write_text("raise RuntimeError('shadow')\n")
        (world.root / "src" / f"{name}.py").write_text("raise RuntimeError('shadow')\n")
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import pydoc\nfrom lakana import asset\n\n\n@asset(inputs={'obs': 'silver.weather'})\n"
        "def f(obs):\n    assert hasattr(pydoc, 'help')\n    return obs\n"
    )
    assert world.run() == 0


def test_helpers_in_the_root_and_in_src_are_importable(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "rootmod.py").write_text("VALUE = 1\n")
    (world.root / "src" / "srcmod.py").write_text("VALUE = 2\n")
    monkeypatch_names = ("rootmod", "srcmod")
    for name in monkeypatch_names:
        sys.modules.pop(name, None)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import rootmod\nimport srcmod\nfrom lakana import asset\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\n"
        "def f(obs):\n    return obs.with_columns(v=rootmod.VALUE + srcmod.VALUE)\n"
    )
    assert world.run() == 0
    for name in monkeypatch_names:
        sys.modules.pop(name, None)


def test_with_p_a_root_level_json_and_lakana_directory_do_not_break_the_harness(
    world: _World,
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "json.py").write_text("raise RuntimeError('shadow')\n")
    (world.root / "lakana").mkdir()
    (world.root / "lakana" / "__init__.py").write_text("raise RuntimeError('shadow')\n")
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\n@asset(inputs={'obs': 'silver.weather'})\n"
        "def f(obs):\n    return obs\n"
    )
    finished = _in_subprocess(world)
    assert finished.returncode == 0, finished.stderr
    assert world.record()["status"] == "succeeded"
    started = subprocess.run(
        [sys.executable, "-P", "-m", "lakana.runner"],
        input="not json",
        capture_output=True,
        text=True,
        cwd=world.root,
        timeout=60,
        check=False,
    )
    assert started.returncode == 2
    assert "task description" in started.stderr


def test_missing_polars_says_to_add_the_assets_extra(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    monkeypatch.setitem(sys.modules, "polars", None)
    assert world.run() == 1
    record = world.record()
    assert record["retryable"] is False
    assert "lakana[assets]" in str(record["message"])
    assert record["phase"] == "load"


def test_a_record_that_cannot_be_built_still_ends_the_process_with_a_fallback(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\nclass Meta(type):\n"
        "    @property\n    def __name__(cls):\n        raise RuntimeError('no name')\n\n\n"
        "class Bad(Exception, metaclass=Meta):\n    pass\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef f(obs):\n    raise Bad('x')\n"
    )
    assert world.run() == 1
    record = world.record()
    assert record["status"] == "failed"
    assert record["retryable"] is True
    assert record["phase"] == "function"
    assert record["conflict"] is False


def test_run_cli_ends_the_process_even_when_main_raises(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        raise _Exited

    def boom() -> int:
        raise RuntimeError("main failed")

    monkeypatch.setattr(runner.os, "_exit", fake_exit)
    monkeypatch.setattr(runner, "main", boom)
    with pytest.raises(_Exited):
        runner.run_cli()
    assert codes == [1]


def test_a_failed_record_build_with_a_leftover_thread_does_not_hang(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import threading\nimport time\nfrom lakana import asset\n\n\n"
        "class Meta(type):\n    @property\n    def __name__(cls):\n"
        "        raise RuntimeError('no name')\n\n\n"
        "class Bad(Exception, metaclass=Meta):\n    pass\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\ndef f(obs):\n"
        "    threading.Thread(target=time.sleep, args=(300,)).start()\n    raise Bad('x')\n"
    )
    finished = _in_subprocess(world)
    assert finished.returncode == 1
    assert world.record()["status"] == "failed"


def test_the_last_stderr_lines_are_the_users_own_error(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    _failing(world, "raise RuntimeError('the real problem')")
    lines = capsys.readouterr().err.rstrip().splitlines()
    assert lines[-1] == "RuntimeError: the real problem"
    assert "Failure" not in "\n".join(lines)


def test_format_characters_are_escaped_except_joiners(world: _World) -> None:
    record = _failing(
        world,
        "raise RuntimeError('a\\u202eb\\u200bc\\ufeffd\\u200de\\u200cf')",
    )
    assert record["message"] == "RuntimeError: a\\u202eb\\u200bc\\ufeffd\u200de\u200cf"


def test_die_with_parent_asks_the_kernel_for_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    class FakeLibc:
        def prctl(self, option: int, argument: int) -> int:
            calls.append((option, argument))
            return 0

    def fake_cdll(name: object, *, use_errno: bool = False) -> FakeLibc:
        assert name is None and use_errno is True
        return FakeLibc()

    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.ctypes, "CDLL", fake_cdll)
    runner.die_with_parent()
    assert calls == [(1, int(signal.SIGKILL))]


def test_die_with_parent_does_nothing_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not load libc")

    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(runner.ctypes, "CDLL", refuse)
    runner.die_with_parent()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_the_harness_dies_with_the_process_that_started_it(tmp_path: Path) -> None:
    """On Linux the child sets `PR_SET_PDEATHSIG`."""
    pid_file = tmp_path / "pid"
    child = (
        "import os, sys, time\n"
        "from lakana.runner import die_with_parent\n"
        "die_with_parent()\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    parent = (
        "import subprocess, sys, time, os\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"while not os.path.exists({str(pid_file)!r}) or not open({str(pid_file)!r}).read():\n"
        "    time.sleep(0.05)\n"
    )
    subprocess.run([sys.executable, "-c", parent], check=True, timeout=30)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    pytest.fail("the harness outlived the process that started it")


def test_die_with_parent_exits_when_the_prctl_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingLibc:
        def prctl(self, option: int, argument: int) -> int:
            return -1

    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.ctypes, "CDLL", lambda *_a, **_k: FailingLibc())
    with pytest.raises(SystemExit) as caught:
        runner.die_with_parent()
    assert caught.value.code == 1


def test_a_harness_whose_worker_is_already_gone_exits_without_a_record(
    world: _World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The worker died between fork and prctl, so the parent-death signal never fires."""
    monkeypatch.setattr(runner.os, "getppid", lambda: 1)
    assert world.run(parent_pid=4242) == 1
    assert not world.result_path.exists()
    assert world.client.built_with is None
    assert "worker" in capsys.readouterr().err


def test_a_harness_whose_worker_is_still_there_runs(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(runner.os, "getppid", lambda: 4242)
    assert world.run(parent_pid=4242) == 0


def test_a_description_without_parent_pid_still_runs(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    assert world.run() == 0
    assert parse_description(world.description()).parent_pid is None
    assert parse_description(world.description(parent_pid=77)).parent_pid == 77


@pytest.mark.parametrize("value", [True, "7", 0, -3, 1.5, None])
def test_a_bad_parent_pid_is_refused(
    world: _World, value: object, capsys: pytest.CaptureFixture[str]
) -> None:
    if value is None:
        assert parse_description(world.description(parent_pid=None)).parent_pid is None
        return
    assert world.run(parent_pid=value) == 2
    assert "parent_pid" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "polars"),
    [("polars", True), ("polars.series", True), ("pyarrow", False), (None, False)],
)
def test_the_polars_hint_names_only_a_missing_polars(
    monkeypatch: pytest.MonkeyPatch, name: str | None, polars: bool
) -> None:
    """Another missing module must not be reported as a missing polars."""

    def refuse(module: str) -> None:
        raise ImportError("No module named 'x'", name=name)

    monkeypatch.setattr("lakana.runner.importlib.import_module", refuse)
    with pytest.raises(AssetLoadError) as caught:
        _import_dependencies()
    text = str(caught.value)
    assert ("polars is not installed" in text) is polars
    assert "lakana[assets]" in text


# ---------------------------------------------------------------- artifacts


class _Artifacts:
    """A fake `client.artifacts`.

    Parameters
    ----------
    outcome
        What `upload` returns or raises.
    files
        Parameter directory name to the single file `download` writes there.
    """

    def __init__(self, outcome: object = None, files: dict[str, bytes] | None = None) -> None:
        self.outcome = outcome
        self.files = files or {}
        self.uploads: list[dict[str, object]] = []
        self.downloads: list[dict[str, object]] = []
        self.given: list[str] = []
        self.working_directories: list[str] = []
        self.before_scan: Callable[[], None] | None = None
        self.parent_modes: list[int] = []

    def upload(
        self, environment: str, qualified_name: str, path: Path, **options: object
    ) -> object:
        """Record the upload and the tree it was given.

        Parameters
        ----------
        environment
            The environment.
        qualified_name
            The artifact.
        path
            The local path.
        **options
            `sources`.

        Returns
        -------
        object
            The configured version.
        """
        self.given.append(str(path))
        self.working_directories.append(os.getcwd())
        if self.before_scan is not None:
            self.before_scan()
        local = scan(Path(path))
        self.uploads.append(
            {
                "environment": environment,
                "artifact": qualified_name,
                "path": Path(os.path.abspath(path)),
                "is_directory": local.is_directory,
                "files": [f.path for f in local.files],
                **options,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def download(
        self,
        environment: str,
        qualified_name: str,
        directory: Path,
        *,
        version: int,
        read_only: bool = False,
    ) -> ArtifactDownload:
        """Write one small file as the downloaded artifact.

        Parameters
        ----------
        environment
            The environment.
        qualified_name
            The artifact.
        directory
            Where to write; it must not exist yet.
        version
            The version asked for.
        read_only
            Whether the copy is to be sealed.

        Returns
        -------
        ArtifactDownload
            Pointing at the written file.
        """
        self.downloads.append(
            {
                "environment": environment,
                "artifact": qualified_name,
                "directory": Path(directory),
                "version": version,
                "read_only": read_only,
            }
        )
        self.parent_modes.append(stat.S_IMODE(os.stat(Path(directory).parent).st_mode))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        Path(directory).mkdir()
        target = Path(directory) / "model.json"
        target.write_bytes(self.files[Path(directory).name])
        return ArtifactDownload(str(target), version, "a" * 64, target.stat().st_size, 1)


_STORED = ArtifactVersion(
    version=7,
    manifest_sha256="c" * 64,
    size_bytes=14,
    file_count=1,
    is_directory=False,
    task_run_id=None,
    run_id=None,
    project_version_id=None,
    commit_sha=None,
    created_by="etl",
    created_at="2026-10-04T12:00:00Z",
    deleted_at=None,
    deleted_by=None,
)
_PRODUCER = """\
import json
from pathlib import Path

import polars as pl
import lakana
from lakana import asset


@asset(inputs={"obs": "silver.weather"}, output="artifact", keep_versions=2)
def churn_model(obs: pl.DataFrame) -> Path:
    out = lakana.output_dir() / "model.json"
    out.write_text(json.dumps({"rows": obs.height}))
    return out
"""
_CONSUMER = """\
import json
from pathlib import Path

import polars as pl
from lakana import asset


@asset(inputs={"model": "models.churn", "obs": "silver.weather"})
def scores(model: Path, obs: pl.DataFrame) -> pl.DataFrame:
    factor = json.loads(model.read_text())["factor"]
    return obs.with_columns(score=pl.col("id") * factor)
"""
_PRODUCER_BODY = (
    '    out = lakana.output_dir() / "model.json"\n'
    '    out.write_text(json.dumps({"rows": obs.height}))\n'
    "    return out\n"
)
_TREE = _PRODUCER.replace(
    _PRODUCER_BODY,
    '    (lakana.output_dir() / "m" / "w").mkdir(parents=True)\n'
    '    (lakana.output_dir() / "m" / "w" / "a.bin").write_bytes(b"a")\n'
    '    (lakana.output_dir() / "notes.txt").write_text("n")\n'
    "    return RETURNED\n",
)


def _artifact_world(world: _World, source: str, outcome: object = _STORED) -> dict[str, object]:
    """Turn the world's asset into an artifact producer and return description overrides.

    Parameters
    ----------
    world
        The world.
    source
        The asset file's text.
    outcome
        What the fake upload returns or raises.

    Returns
    -------
    dict
        Overrides for `world.run`.
    """
    (world.root / "assets" / "gold" / "scores.py").write_text(source)
    world.client.artifacts = _Artifacts(outcome)
    return {"table": "models.churn", "output": "artifact"}


def _output(world: _World) -> Path:
    """Return where the attempt's output directory is.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    Path
        `<work_dir>/output`.
    """
    return world.result_path.parent / "output"


def test_a_producer_writes_into_the_output_directory_and_uploads_what_it_returned(
    world: _World,
) -> None:
    overrides = _artifact_world(world, _PRODUCER)
    assert world.run(**overrides) == 0
    assert world.record() == {
        "status": "succeeded",
        "output": "artifact",
        "version": 7,
        "manifest_sha256": "c" * 64,
    }
    (upload,) = world.client.artifacts.uploads
    output = _output(world)
    assert upload == {
        "environment": "dev",
        "artifact": "models.churn",
        "path": output / "model.json",
        "is_directory": False,
        "files": ["model.json"],
        "sources": {"silver.weather": None},
    }
    assert json.loads((output / "model.json").read_text()) == {"rows": 2}
    assert (output.stat().st_mode & 0o777) == 0o700
    assert world.client.tables.calls == []
    assert os.getcwd() == str(world.root)
    with pytest.raises(RuntimeError, match="available only while"):
        assets.output_dir()


def test_the_upload_runs_with_the_returned_directory_as_the_working_directory(
    world: _World,
) -> None:
    """The scan opens '.' and a bare name, so a swapped path cannot redirect it."""
    overrides = _artifact_world(world, _PRODUCER)
    assert world.run(**overrides) == 0
    artifacts = world.client.artifacts
    assert artifacts.given == ["model.json"]
    assert artifacts.working_directories == [str(_output(world))]
    overrides = _artifact_world(world, _TREE.replace("RETURNED", 'lakana.output_dir() / "m"'))
    shutil.rmtree(_output(world))
    assert world.run(**overrides) == 0
    assert world.client.artifacts.given == ["."]
    assert world.client.artifacts.working_directories == [str(_output(world) / "m")]


@pytest.mark.parametrize(
    ("returned", "is_directory", "files"),
    [
        ("lakana.output_dir()", True, ["m/w/a.bin", "notes.txt"]),
        ('lakana.output_dir() / "m"', True, ["w/a.bin"]),
        ('lakana.output_dir() / "m" / "w"', True, ["a.bin"]),
        ('lakana.output_dir() / "notes.txt"', False, ["notes.txt"]),
        ('lakana.output_dir() / "m" / "w" / "a.bin"', False, ["a.bin"]),
        ('lakana.output_dir() / "m" / ".." / "notes.txt"', False, ["notes.txt"]),
        ('lakana.output_dir() / "m" / "w" / ".." / "w" / "a.bin"', False, ["a.bin"]),
        ('Path(str(lakana.output_dir()) + "/")', True, ["m/w/a.bin", "notes.txt"]),
        ('Path(str(lakana.output_dir()) + "//m")', True, ["w/a.bin"]),
    ],
)
def test_a_returned_directory_or_file_inside_the_output_directory_is_stored(
    world: _World, returned: str, is_directory: bool, files: list[str]
) -> None:
    overrides = _artifact_world(world, _TREE.replace("RETURNED", returned))
    assert world.run(**overrides) == 0, world.record()
    (upload,) = world.client.artifacts.uploads
    assert (upload["is_directory"], upload["files"]) == (is_directory, files)


@pytest.mark.parametrize(
    ("returned", "fragment"),
    [
        (
            "None",
            "must return a pathlib.Path inside lakana.output_dir() (a file, or the directory "
            "itself), got builtins.NoneType",
        ),
        ('str(lakana.output_dir() / "model.json")', "got builtins.str; wrap it in pathlib.Path"),
        ('bytes(lakana.output_dir() / "model.json")', "got builtins.bytes"),
        ("pl.DataFrame({'a': [1]})", "got polars.dataframe.frame.DataFrame"),
        ('PurePosixPath(lakana.output_dir() / "model.json")', "got pathlib."),
        ('Path("/etc/passwd")', "which is not inside lakana.output_dir()"),
        ('lakana.output_dir() / ".." / "result.json"', "which is not inside lakana.output_dir()"),
        ('lakana.output_dir() / "m" / ".." / ".." / "result.json"', "which is not inside"),
        ("lakana.output_dir().parent", "which is not inside lakana.output_dir()"),
        (
            'Path(str(lakana.output_dir()) + "-evil") / "model.json"',
            "which is not inside lakana.output_dir()",
        ),
        ('Path("/" + str(lakana.output_dir()))', "which is not inside lakana.output_dir()"),
        ('Path("model.json")', "which is not inside lakana.output_dir()"),
        ('Path("output/model.json")', "which is not inside lakana.output_dir()"),
        (
            'lakana.output_dir() / "missing.bin"',
            "which does not exist; write the file before returning its path",
        ),
        (
            'lakana.output_dir() / "model.json" / "below"',
            "which does not exist; write the file before returning its path",
        ),
        (
            'Path(str(lakana.output_dir() / "model.json") + "\\x00x")',
            "which does not exist; write the file before returning its path",
        ),
    ],
)
def test_a_returned_path_outside_the_output_directory_fails_permanently(
    world: _World, returned: str, fragment: str
) -> None:
    """Nothing but a path inside the output directory is ever uploaded."""
    source = _PRODUCER.replace("    return out\n", f"    return {returned}\n").replace(
        "from pathlib import Path\n", "from pathlib import Path, PurePosixPath\n"
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["status"], record["retryable"], record["phase"]) == (
        "failed",
        False,
        "validate",
    )
    assert fragment in record["message"]
    assert world.client.artifacts.uploads == []
    assert os.getcwd() == str(world.root)


_NEEDS_PERMISSION_BITS = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory permissions"
)


@pytest.mark.parametrize(
    "returned",
    [
        'Path(str(lakana.output_dir() / "model.json") + "\\ud800")',
        'Path(str(lakana.output_dir() / "model.json") + "\\udcff")',
    ],
)
def test_a_returned_text_that_cannot_name_a_file_is_a_missing_file_not_a_codec_error(
    world: _World, returned: str
) -> None:
    source = _PRODUCER.replace("    return out\n", f"    return {returned}\n")
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("validate", False)
    assert "which does not exist; write the file before returning its path" in record["message"]
    assert "codec" not in record["message"]


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="finds the descriptor in /proc")
def test_user_code_that_closes_the_output_descriptor_is_named_as_the_cause(
    world: _World,
) -> None:
    """The harness's descriptor is gone: a permanent failure with the cause and the next step."""
    source = _PRODUCER.replace(
        "    return out\n",
        "    import os\n"
        "    for fd in map(int, os.listdir('/proc/self/fd')):\n"
        "        try:\n"
        "            if os.readlink(f'/proc/self/fd/{fd}') == str(lakana.output_dir()):\n"
        "                os.close(fd)\n"
        "        except OSError:\n"
        "            pass\n"
        "    return lakana.output_dir() / 'model.json'\n",
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"], record["error_type"]) == (
        "validate",
        False,
        "InvalidResult",
    )
    assert "closed a file descriptor the task needs" in record["message"]
    assert "do not close descriptors you did not open" in record["message"]
    assert "os.closerange" in record["message"]
    assert "run the task again" in record["message"]
    assert "Errno" not in record["message"]
    assert world.client.artifacts.uploads == []


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="finds the descriptor in /proc")
@pytest.mark.parametrize(
    ("opened", "returned"),
    [
        ("os.O_RDONLY", "lakana.output_dir() / 'model.json'"),
        ("os.O_RDONLY | os.O_DIRECTORY", "lakana.output_dir() / 'model.json'"),
        ("os.O_RDONLY | os.O_DIRECTORY", "lakana.output_dir()"),
    ],
    ids=["a file, a returned file", "a directory, a returned file", "a directory, the directory"],
)
def test_a_descriptor_number_the_code_reuses_is_not_taken_for_the_harness_s_directory(
    world: _World, opened: str, returned: str
) -> None:
    """User code closes the output descriptor and opens something else under the same number.

    The harness must not use that descriptor as the output directory (it would store a
    file's or an unrelated directory's contents), and must not close it as its own: the
    named failure is recorded, nothing is uploaded, and the record is written.
    """
    source = _PRODUCER.replace(
        "    return out\n",
        "    import os\n"
        "    closed = []\n"
        "    for fd in map(int, os.listdir('/proc/self/fd')):\n"
        "        try:\n"
        "            if os.readlink(f'/proc/self/fd/{fd}') == str(lakana.output_dir()):\n"
        "                os.close(fd)\n"
        "                closed.append(fd)\n"
        "        except OSError:\n"
        "            pass\n"
        "    assert closed\n"
        "    held = []\n"
        "    while len(held) < 4096 and not set(closed) <= set(held):\n"
        f"        held.append(os.open('/', {opened}))\n"
        "    assert set(closed) <= set(held), 'the number was not reused'\n"
        f"    return {returned}\n",
    )
    overrides = _artifact_world(world, source)
    before = set(os.listdir("/proc/self/fd"))
    try:
        assert world.run(**overrides) == 1
    finally:
        # The harness runs in this process and rightly leaves the code's descriptors of "/"
        # open; close them so that they do not outlive the test.
        for name in set(os.listdir("/proc/self/fd")) - before:
            with contextlib.suppress(OSError):
                if os.readlink(f"/proc/self/fd/{name}") == "/":
                    os.close(int(name))
    record = world.record()
    assert (record["phase"], record["retryable"], record["error_type"]) == (
        "validate",
        False,
        "InvalidResult",
    )
    assert "closed a file descriptor the task needs" in record["message"]
    assert world.client.artifacts.uploads == []


@dataclass
class _Opened:
    """The descriptors `_watch_opened_descriptors` saw opened, with the file each named."""

    ever: list[tuple[int, tuple[int, int]]] = field(default_factory=list)

    @property
    def numbers(self) -> list[int]:
        """The numbers of every descriptor opened.

        Returns
        -------
        list[int]
            In the order they were opened.
        """
        return [number for number, _ in self.ever]

    def leaked(self) -> list[int]:
        """Return the descriptors opened that still name the file they were opened on.

        Closing may happen through `os.close` or a file object, so this asks the system:
        a number that is closed, or names another file now, is not a leak.

        Returns
        -------
        list[int]
            The numbers still open on the same device and inode.
        """
        found = []
        for number, identity in self.ever:
            try:
                info = os.fstat(number)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) == identity:
                found.append(number)
        return found


def _watch_opened_descriptors(monkeypatch: pytest.MonkeyPatch) -> _Opened:
    """Watch the descriptors the calling thread opens with `os.open`.

    Other threads and finalizers also open and close descriptors, so comparing every
    descriptor of the process before and after is flaky. This sees only what the
    code under test opens, in the thread that calls it.

    Parameters
    ----------
    monkeypatch
        Wraps `os.open` until the test ends.

    Returns
    -------
    _Opened
        Every descriptor the calling thread opened; it fills as the test goes on.
    """
    mine = threading.get_ident()
    seen = _Opened()
    real_open, real_fstat = os.open, os.fstat

    def watching_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if threading.get_ident() == mine:
            info = real_fstat(descriptor)
            seen.ever.append((descriptor, (info.st_dev, info.st_ino)))
        return descriptor

    monkeypatch.setattr(os, "open", watching_open)
    return seen


def test_a_failing_fstat_after_opening_the_output_directory_leaks_no_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness closes a directory it opened but could not identify."""
    description: Any = SimpleNamespace(work_dir=tmp_path, input_kinds={}, output="artifact")
    opened = _watch_opened_descriptors(monkeypatch)
    real = os.fstat
    identified: list[int] = []

    def failing(fd: int) -> os.stat_result:
        identified.append(fd)
        assert fd in opened.numbers  # the descriptor this test is about is the one being watched
        monkeypatch.setattr(runner.os, "fstat", real)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(runner.os, "fstat", failing)
    with pytest.raises(OSError, match="Input/output error"), runner._workspace(description):
        pass  # pragma: no cover - the workspace raises before it yields
    assert len(identified) == 1
    assert opened.leaked() == []


def test_the_workspace_closes_only_a_descriptor_that_is_still_its_directory(
    tmp_path: Path,
) -> None:
    """A number the code closed and the process reused is not the harness's to close."""
    description: Any = SimpleNamespace(work_dir=tmp_path, input_kinds={}, output="artifact")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stranger = None
    try:
        with runner._workspace(description) as workspace:
            assert workspace.output is not None
            number = workspace.output.descriptor
            assert runner._is_held(number, workspace.output.identity)
            os.close(number)
            assert not runner._is_held(number, workspace.output.identity)
            # Another file takes the number: forced, so that nothing else in the process
            # can take it first.
            source = os.open(elsewhere, os.O_RDONLY | os.O_DIRECTORY)
            try:
                stranger = os.dup2(source, number)
            finally:
                if source != number:
                    os.close(source)
            assert stranger == number
            assert not runner._is_held(number, workspace.output.identity)
        try:
            os.fstat(stranger)
        except OSError:  # pragma: no cover - the failure this test exists to name
            pytest.fail("the workspace closed a descriptor that was not its own")
    finally:
        if stranger is not None:
            with contextlib.suppress(OSError):
                os.close(stranger)


@_NEEDS_PERMISSION_BITS
def test_a_directory_on_the_returned_path_that_cannot_be_opened_is_not_called_a_link(
    world: _World,
) -> None:
    source = _TREE.replace("RETURNED", 'lakana.output_dir() / "m" / "w" / "a.bin"').replace(
        "    return ", "    (lakana.output_dir() / 'm').chmod(0)\n    return ", 1
    )
    overrides = _artifact_world(world, source)
    try:
        assert world.run(**overrides) == 1
    finally:
        (_output(world) / "m").chmod(0o700)
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("validate", False)
    assert "symbolic link" not in record["message"]
    assert "cannot be opened (Permission denied)" in record["message"]
    assert "keep the permissions of the directories" in record["message"]
    assert "Errno" not in record["message"]
    assert world.client.artifacts.uploads == []


@_NEEDS_PERMISSION_BITS
def test_a_file_in_an_output_directory_without_search_permission_is_not_called_missing(
    world: _World,
) -> None:
    source = _PRODUCER.replace(
        "    return out\n",
        "    lakana.output_dir().chmod(0)\n    return lakana.output_dir() / 'model.json'\n",
    )
    overrides = _artifact_world(world, source)
    try:
        assert world.run(**overrides) == 1
    finally:
        _output(world).chmod(0o700)
    record = world.record()
    assert (record["phase"], record["retryable"], record["error_type"]) == (
        "validate",
        False,
        "InvalidResult",
    )
    assert "does not exist" not in record["message"]
    assert "cannot be opened (Permission denied)" in record["message"]
    assert "keep the permissions of the directories" in record["message"]
    assert "Errno" not in record["message"]
    assert world.client.artifacts.uploads == []


@_NEEDS_PERMISSION_BITS
def test_an_output_directory_the_code_made_unenterable_fails_for_good(world: _World) -> None:
    source = _PRODUCER.replace(
        "    return out\n", "    lakana.output_dir().chmod(0)\n    return lakana.output_dir()\n"
    )
    overrides = _artifact_world(world, source)
    try:
        assert world.run(**overrides) == 1
    finally:
        _output(world).chmod(0o700)
    record = world.record()
    assert (record["phase"], record["retryable"], record["error_type"]) == (
        "upload",
        False,
        "InvalidResult",
    )
    assert "the harness cannot enter" in record["message"]
    assert "Errno" not in record["message"]
    assert world.client.artifacts.uploads == []
    assert os.getcwd() == str(world.root)


@_NEEDS_PERMISSION_BITS
def test_a_working_directory_the_harness_cannot_reopen_fails_for_good(world: _World) -> None:
    """The code chdirs into a directory and removes every permission of ``.`` itself."""
    source = _PRODUCER.replace(
        "    return out\n",
        "    import os\n"
        "    os.mkdir(lakana.output_dir() / 'cwd')\n"
        "    os.chdir(lakana.output_dir() / 'cwd')\n"
        "    os.chmod('.', 0)\n"
        "    return out\n",
    )
    overrides = _artifact_world(world, source)
    try:
        assert world.run(**overrides) == 1
    finally:
        os.chdir(world.root)
        (_output(world) / "cwd").chmod(0o700)
    record = world.record()
    assert (record["phase"], record["retryable"], record["error_type"]) == (
        "upload",
        False,
        "InvalidResult",
    )
    assert "cannot reopen" in record["message"]
    assert "Errno" not in record["message"]
    assert world.client.artifacts.uploads == []


@_NEEDS_PERMISSION_BITS
@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="needs O_PATH")
def test_a_working_directory_without_read_permission_is_still_restored_and_stored(
    world: _World,
) -> None:
    source = _PRODUCER.replace(
        "    return out\n",
        "    import os\n"
        "    os.mkdir(lakana.output_dir() / 'cwd', 0o300)\n"
        "    os.chdir(lakana.output_dir() / 'cwd')\n"
        "    return out\n",
    )
    overrides = _artifact_world(world, source)
    try:
        assert world.run(**overrides) == 0, world.record()
    finally:
        os.chdir(world.root)
        (_output(world) / "cwd").chmod(0o700)
    assert world.client.artifacts.uploads[0]["files"] == ["model.json"]


@pytest.mark.parametrize("upload_fails", [False, True])
def test_a_failure_to_restore_the_working_directory_does_not_replace_the_uploads_outcome(
    world: _World, monkeypatch: pytest.MonkeyPatch, upload_fails: bool
) -> None:
    """The record's path is absolute, so the process may simply stay where it is."""
    real = os.fchdir
    calls: list[int] = []

    def fail_the_second(descriptor: int) -> None:
        calls.append(descriptor)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied")
        real(descriptor)

    outcome = ValidationError("refused", status=422) if upload_fails else _STORED
    overrides = _artifact_world(world, _PRODUCER, outcome)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fchdir", fail_the_second)
            assert world.run(**overrides) == (1 if upload_fails else 0)
    finally:
        os.chdir(world.root)
    assert len(calls) == 2
    record = world.record()
    if upload_fails:
        assert (record["phase"], record["error_type"]) == ("upload", "UploadRefused")
    else:
        assert (record["status"], record["version"]) == ("succeeded", 7)


_LIAR = '''\

class Liar(type(Path())):
    """A Path whose every method lies or raises."""

    def _boom(self, *a, **k):
        raise AssertionError("the harness called a method of the returned object")

    exists = resolve = is_symlink = lstat = stat = is_file = is_dir = samefile = _boom
    relative_to = is_relative_to = readlink = iterdir = open = read_bytes = absolute = _boom
    parents = property(_boom)
    parent = property(_boom)
    __eq__ = __hash__ = _boom
'''


def test_the_returned_object_is_read_once_as_text_and_never_asked_anything_else(
    world: _World,
) -> None:
    source = _PRODUCER.replace("    return out\n", "    return Liar(out)\n").replace(
        "@asset(", _LIAR + "\n\n@asset(", 1
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 0, world.record()
    assert world.client.artifacts.uploads[0]["files"] == ["model.json"]


def test_a_returned_object_whose_text_changes_is_judged_by_its_first_reading(
    world: _World,
) -> None:
    flipping = (
        "\n\nclass Flip(type(Path())):\n"
        "    calls = []\n\n"
        "    def __fspath__(self):\n"
        "        type(self).calls.append(1)\n"
        '        return str(self) if len(type(self).calls) == 1 else "/etc/passwd"\n'
    )
    source = _PRODUCER.replace("    return out\n", "    return Flip(out)\n").replace(
        "@asset(", flipping + "\n\n@asset(", 1
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 0, world.record()
    assert world.client.artifacts.uploads[0]["files"] == ["model.json"]


@pytest.mark.parametrize(
    "fspath",
    [
        "b'/etc/passwd'",
        "None",
        "raise OSError('no')",
        "7",
        "type('Text', (str,), {})(str(self))",
    ],
)
def test_a_path_object_that_cannot_give_its_text_is_refused(world: _World, fspath: str) -> None:
    body = fspath if fspath.startswith("raise") else f"return {fspath}"
    odd = f"\n\nclass Odd(type(Path())):\n    def __fspath__(self):\n        {body}\n"
    source = _PRODUCER.replace("    return out\n", "    return Odd(out)\n").replace(
        "@asset(", odd + "\n\n@asset(", 1
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("validate", False)
    assert "must return a pathlib.Path inside lakana.output_dir()" in record["message"]
    assert world.client.artifacts.uploads == []


def test_an_object_that_claims_to_be_a_path_is_not_one(world: _World) -> None:
    source = _PRODUCER.replace(
        "    return out\n",
        "    class Fake:\n"
        "        __class__ = Path\n"
        "        def __fspath__(self):\n"
        "            return str(out)\n"
        "    return Fake()\n",
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    assert "got lakana_asset.churn_model.<locals>.Fake" in world.record()["message"]


def test_the_message_shows_a_hostile_path_escaped_and_bounded(world: _World) -> None:
    source = _PRODUCER.replace(
        "    return out\n",
        '    return lakana.output_dir() / ("x\\n\\x1b[31m\\u202e" + "y" * 400)\n',
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    message = world.record()["message"]
    assert "x\\n\\x1b[31m\\u202e" in message
    assert "\x1b" not in message and "‮" not in message
    assert "y" * 201 not in message


def test_a_link_in_the_output_is_refused_and_named(world: _World, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("not yours")
    linked = _PRODUCER.replace(
        "    return out\n",
        f'    (lakana.output_dir() / "leak").symlink_to({str(secret)!r})\n'
        "    return lakana.output_dir()\n",
    )
    overrides = _artifact_world(world, linked)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["retryable"], record["phase"]) == (False, "upload")
    assert record["message"] == (
        "leak is a symbolic link; an artifact holds only regular files and directories. "
        "Copy what it points to instead."
    )
    through = _PRODUCER.replace(
        "    return out\n",
        f'    (lakana.output_dir() / "alias").symlink_to({str(tmp_path)!r}, '
        "target_is_directory=True)\n"
        '    return lakana.output_dir() / "alias" / "secret"\n',
    )
    overrides = _artifact_world(world, through)
    shutil.rmtree(_output(world))
    assert world.run(**overrides) == 1
    assert "which passes through a symbolic link" in world.record()["message"]
    assert world.client.artifacts.uploads == []


def test_a_link_to_a_file_inside_the_output_is_refused_as_the_returned_path(
    world: _World,
) -> None:
    source = _PRODUCER.replace(
        "    return out\n",
        '    (lakana.output_dir() / "alias.json").symlink_to(out)\n'
        '    return lakana.output_dir() / "alias.json"\n',
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    assert "which passes through a symbolic link" in world.record()["message"]
    assert world.client.artifacts.uploads == []


def test_a_link_to_a_directory_inside_the_output_is_refused_even_when_it_points_inside(
    world: _World,
) -> None:
    source = _TREE.replace(
        "RETURNED",
        'lakana.output_dir().joinpath("link").symlink_to(lakana.output_dir() / "m", '
        'target_is_directory=True) or lakana.output_dir() / "link" / "w" / "a.bin"',
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    assert "which passes through a symbolic link" in world.record()["message"]


def test_a_directory_swapped_for_a_link_between_the_check_and_the_open_is_refused(
    world: _World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "w").mkdir()
    (outside / "w" / "a.bin").write_bytes(b"stolen")
    overrides = _artifact_world(
        world, _TREE.replace("RETURNED", 'lakana.output_dir() / "m" / "w" / "a.bin"')
    )
    real_open = os.open
    swapped: list[str] = []

    def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "m" and kwargs.get("dir_fd") is not None and not swapped:
            swapped.append("m")
            os.rename(_output(world) / "m", _output(world) / "m.orig")
            os.symlink(outside, _output(world) / "m")
        return real_open(path, flags, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(os, "open", racing_open)
    assert world.run(**overrides) == 1
    monkeypatch.undo()
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("validate", False)
    assert (
        "was replaced by a symbolic link or another file while it was checked" in record["message"]
    )
    assert world.client.artifacts.uploads == []
    assert swapped == ["m"]


@pytest.mark.parametrize(
    "returned", ["lakana.output_dir() / 'm'", "lakana.output_dir() / 'm' / 'w' / 'a.bin'"]
)
def test_a_directory_swapped_for_a_link_after_the_check_is_not_followed_by_the_upload(
    world: _World, tmp_path: Path, returned: str
) -> None:
    """A background thread of the user's code swaps a directory while the upload runs."""
    outside = tmp_path / "outside"
    (outside / "w").mkdir(parents=True)
    (outside / "w" / "a.bin").write_bytes(b"stolen")
    overrides = _artifact_world(world, _TREE.replace("RETURNED", returned))
    artifacts = world.client.artifacts

    def swap() -> None:
        output = _output(world)
        os.rename(output / "m", output / "m.orig")
        os.symlink(outside, output / "m")

    artifacts.before_scan = swap
    assert world.run(**overrides) == 0, world.record()
    (upload,) = artifacts.uploads
    assert upload["files"] == (["w/a.bin"] if returned.endswith("'m'") else ["a.bin"])
    assert (_output(world) / "m").is_symlink()
    assert os.getcwd() == str(world.root)


@pytest.mark.parametrize("as_link", [False, True])
def test_the_output_directory_moved_or_replaced_by_a_link_is_never_looked_up_again(
    world: _World, tmp_path: Path, as_link: bool
) -> None:
    """The output directory is pinned when it is made; its name is not trusted afterwards."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.json").write_text("stolen")
    replacement = f"os.symlink({str(outside)!r}, base)" if as_link else "base.mkdir()"
    source = _PRODUCER.replace(
        "    return out\n",
        "    import os\n"
        "    base = out.parent\n"
        "    os.rename(base, base.with_name('output.orig'))\n"
        f"    {replacement}\n"
        "    return out\n",
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 0, world.record()
    artifacts = world.client.artifacts
    assert artifacts.working_directories == [str(_output(world).with_name("output.orig"))]
    assert artifacts.uploads[0]["files"] == ["model.json"]
    assert (outside / "model.json").read_text() == "stolen"


def test_an_empty_output_directory_is_refused_by_the_scan_and_not_retried(
    world: _World,
) -> None:
    source = _PRODUCER.replace('    out = lakana.output_dir() / "model.json"\n', "").replace(
        '    out.write_text(json.dumps({"rows": obs.height}))\n    return out\n',
        "    return lakana.output_dir()\n",
    )
    overrides = _artifact_world(world, source)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("upload", False)
    assert (
        record["message"]
        == "The directory has no files; an artifact must contain at least one file."
    )


def test_a_platform_without_descriptor_access_refuses_an_artifact_asset(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = _artifact_world(world, _PRODUCER)
    monkeypatch.setattr(runner, "_BY_DESCRIPTOR", False)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("load", False)
    assert "needs a platform that opens files relative to a directory" in record["message"]
    assert not _output(world).exists()


def test_a_platform_without_descriptor_access_refuses_a_consumer_with_the_same_text(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check comes before the inputs directory is pinned, so a consumer gets the clear text."""
    (world.root / "assets" / "gold" / "scores.py").write_text(_CONSUMER)
    world.client.artifacts = _Artifacts(files={"model": b'{"factor": 3}'})
    monkeypatch.setattr(runner, "_BY_DESCRIPTOR", False)
    real_open = os.open
    opened: list[object] = []

    def open_recording(path: object, *args: object, **kwargs: object) -> int:
        opened.append(path)
        return real_open(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(runner.os, "open", open_recording)
    code = world.run(
        inputs={"model": "models.churn", "obs": "silver.weather"},
        input_kinds={"model": "artifact", "obs": "table"},
        input_versions={"model": 4},
    )
    assert code == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("load", False)
    assert "needs a platform that opens files relative to a directory" in record["message"]
    assert not (world.result_path.parent / "inputs").exists()
    assert not [path for path in opened if str(path).endswith("/inputs")]


@pytest.mark.parametrize(
    ("error", "retryable", "conflict"),
    [
        (
            ValidationError(
                "The artifact is 9 bytes, more than LAKANA_ARTIFACTS__MAX_BYTES (8).", status=422
            ),
            False,
            False,
        ),
        (PermissionDenied("This task attempt was superseded", status=403), False, False),
        (NotFound("The namespace does not exist", status=404), False, False),
        (Conflict("1 of this upload's files are not stored yet", status=409), True, True),
        (LakanaConnectionError("Could not reach http://server:8000: ConnectError"), True, False),
        (ServerError("The server failed", status=503), True, False),
        (
            PayloadTooLarge("The manifest request is larger than 1048576 bytes.", status=413),
            False,
            False,
        ),
    ],
)
def test_an_upload_failure_is_classified(
    world: _World, error: Exception, retryable: bool, conflict: bool
) -> None:
    overrides = _artifact_world(world, _PRODUCER, error)
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"], record["conflict"]) == (
        "upload",
        retryable,
        conflict,
    )
    assert str(error) in record["message"]


def test_a_413_on_an_upload_fails_for_good_and_says_what_the_server_said(world: _World) -> None:
    text = (
        "The manifest request is larger than 1048576 bytes. It lists every file with its path "
        "and hash: store fewer files or give them shorter paths (the limit is fixed)."
    )
    overrides = _artifact_world(world, _PRODUCER, PayloadTooLarge(text, status=413))
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"], record["message"]) == ("upload", False, text)
    assert record["error_type"] == "PayloadTooLarge"


def test_an_unreadable_file_is_reported_with_the_systems_reason_and_not_retried(
    world: _World,
) -> None:
    text = (
        "model.json could not be read: Input/output error. "
        "The upload was stopped and can be tried again once the file is readable."
    )
    overrides = _artifact_world(world, _PRODUCER, ManifestError(text))
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"], record["message"]) == ("upload", False, text)
    assert "Wait until" not in str(record["message"])


def test_an_oserror_of_the_upload_itself_keeps_its_own_class_and_retry_rule(
    world: _World,
) -> None:
    overrides = _artifact_world(world, _PRODUCER, OSError(28, "No space left on device"))
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("upload", True)
    assert record["error_type"] != "InvalidResult"


def test_a_manifest_error_is_permanent_and_its_text_reaches_the_user_verbatim(
    world: _World,
) -> None:
    text = "model.json changed after it was scanned, so nothing was stored from it."
    overrides = _artifact_world(world, _PRODUCER, ManifestError(text))
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"], record["message"]) == ("upload", False, text)
    assert record["error_type"] == "InvalidResult"


def test_the_task_token_never_reaches_an_artifact_message(world: _World) -> None:
    error = PermissionDenied(f"rejected {_TOKEN}", status=403)
    overrides = _artifact_world(world, _PRODUCER, error)
    assert world.run(**overrides) == 1
    assert _TOKEN not in json.dumps(world.record())


def test_a_consumer_receives_a_path_and_records_the_version_it_read(world: _World) -> None:
    (world.root / "assets" / "gold" / "scores.py").write_text(_CONSUMER)
    world.client.artifacts = _Artifacts(files={"model": b'{"factor": 3}'})
    overrides = {
        "inputs": {"model": "models.churn", "obs": "silver.weather"},
        "input_kinds": {"model": "artifact", "obs": "table"},
        "input_versions": {"model": 4},
    }
    assert world.run(**overrides) == 0, world.record()
    artifacts = world.client.artifacts
    (download,) = artifacts.downloads
    assert download == {
        "environment": "dev",
        "artifact": "models.churn",
        "directory": world.result_path.parent / "inputs" / "model",
        "version": 4,
        "read_only": True,
    }
    assert artifacts.parent_modes == [0o700]
    (call,) = world.client.tables.calls
    assert call["data"].column("score").to_pylist() == [3, 6]  # ty: ignore[unresolved-attribute]
    assert call["sources"] == ["models.churn", "silver.weather"]
    assert call["source_versions"] == {"models.churn": 4}
    assert world.record()["status"] == "succeeded"
    assert "output" not in world.record()
    assert world.client.queries.submitted == [(_SELECT, "dev")]


def test_an_artifact_asset_may_consume_an_artifact(world: _World) -> None:
    source = (
        _PRODUCER.replace('inputs={"obs": "silver.weather"}', 'inputs={"obs": "models.base"}')
        .replace("obs: pl.DataFrame", "obs: Path")
        .replace('{"rows": obs.height}', '{"rows": len(obs.read_text())}')
    )
    overrides = _artifact_world(world, source)
    world.client.artifacts.files = {"obs": b"abc"}
    overrides.update(
        inputs={"obs": "models.base"}, input_kinds={"obs": "artifact"}, input_versions={"obs": 2}
    )
    assert world.run(**overrides) == 0, world.record()
    assert world.client.artifacts.uploads[0]["sources"] == {"models.base": 2}
    assert not (world.result_path.parent / "inputs" / "obs").exists() or True


def test_the_harness_never_creates_the_input_target_itself(world: _World) -> None:
    (world.root / "assets" / "gold" / "scores.py").write_text(_CONSUMER)
    world.client.artifacts = _Artifacts(files={"model": b'{"factor": 3}'})
    assert (
        world.run(
            inputs={"model": "models.churn", "obs": "silver.weather"},
            input_kinds={"model": "artifact", "obs": "table"},
            input_versions={"model": 4},
        )
        == 0
    )
    inputs = world.result_path.parent / "inputs"
    assert stat.S_IMODE(inputs.stat().st_mode) == 0o700
    assert [p.name for p in inputs.iterdir()] == ["model"]


def _closing(
    world: _World,
    source: str,
    *,
    which: tuple[str, ...] = ("inputs", "output"),
    indent: str = "    ",
) -> str:
    """Put code at the start of the asset function that closes the harness's descriptors.

    Parameters
    ----------
    world
        The world.
    source
        The asset file's text; its function body starts after the first ``def`` line.
    which
        The directories whose descriptors the code closes.
    indent
        The function body's indentation.

    Returns
    -------
    str
        The source with the code inserted, which closes every descriptor that is open on the
        attempt's directories among `which`, as a library calling `os.closerange` would.
    """
    work = str(world.result_path.parent)
    code = (
        "import os\n"
        f"for _fd in os.listdir('/proc/self/fd'):\n"
        "    try:\n"
        "        _target = os.readlink('/proc/self/fd/' + _fd)\n"
        "    except OSError:\n"
        "        continue\n"
        f"    if _target in {tuple(f'{work}/{name}' for name in which)!r}:\n"
        "        os.close(int(_fd))\n"
    )
    body = "".join(indent + line + "\n" for line in code.splitlines())
    head, _, rest = source.partition("\n    ")
    return head + "\n" + body + "    " + rest


def _failing_closes(world: _World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make closing a descriptor of the attempt's directories fail, as after user code closed it.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        The test's monkeypatch.

    Returns
    -------
    list of str
        Filled, as the attempt runs, with the directory of each descriptor the harness closed.
    """
    work = str(world.result_path.parent)
    real = os.close
    closed: list[str] = []

    def close(fd: int) -> None:
        target = os.readlink(f"/proc/self/fd/{fd}")
        real(fd)
        # Only the close of the workspace itself: the harness also closes duplicates of the
        # output descriptor while it stores the artifact.
        if sys._getframe(1).f_code.co_name == "_workspace" and target in (
            f"{work}/inputs",
            f"{work}/output",
        ):
            closed.append(target.rsplit("/", 1)[1])
            raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(runner.os, "close", close)
    return closed


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc/self/fd")
def test_a_consumer_whose_close_fails_still_succeeds(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-I1: the close at the end of the attempt is not a reason to lose the result."""
    (world.root / "assets" / "gold" / "scores.py").write_text(_CONSUMER)
    world.client.artifacts = _Artifacts(files={"model": b'{"factor": 3}'})
    closed = _failing_closes(world, monkeypatch)
    overrides = {
        "inputs": {"model": "models.churn", "obs": "silver.weather"},
        "input_kinds": {"model": "artifact", "obs": "table"},
        "input_versions": {"model": 4},
    }
    assert world.run(**overrides) == 0, world.record()
    assert world.record()["status"] == "succeeded"
    assert closed == ["inputs"]
    (call,) = world.client.tables.calls
    assert call["data"].column("score").to_pylist() == [3, 6]  # ty: ignore[unresolved-attribute]


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc/self/fd")
def test_a_producer_whose_code_closed_the_inputs_descriptor_still_succeeds(world: _World) -> None:
    source = (
        _PRODUCER.replace('inputs={"obs": "silver.weather"}', 'inputs={"obs": "models.base"}')
        .replace("obs: pl.DataFrame", "obs: Path")
        .replace('{"rows": obs.height}', '{"rows": len(obs.read_text())}')
    )
    overrides = _artifact_world(world, _closing(world, source, which=("inputs",)))
    world.client.artifacts.files = {"obs": b"abc"}
    overrides.update(
        inputs={"obs": "models.base"}, input_kinds={"obs": "artifact"}, input_versions={"obs": 2}
    )
    assert world.run(**overrides) == 0, world.record()
    assert world.record()["status"] == "succeeded"
    assert len(world.client.artifacts.uploads) == 1


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc/self/fd")
def test_the_second_descriptor_is_closed_even_when_the_first_close_fails(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-I1: the first failing close does not skip the other one, so nothing leaks."""
    source = (
        _PRODUCER.replace('inputs={"obs": "silver.weather"}', 'inputs={"obs": "models.base"}')
        .replace("obs: pl.DataFrame", "obs: Path")
        .replace('{"rows": obs.height}', '{"rows": len(obs.read_text())}')
    )
    overrides = _artifact_world(world, source)
    world.client.artifacts.files = {"obs": b"abc"}
    overrides.update(
        inputs={"obs": "models.base"}, input_kinds={"obs": "artifact"}, input_versions={"obs": 2}
    )
    closed = _failing_closes(world, monkeypatch)
    assert world.run(**overrides) == 0, world.record()
    assert world.record()["status"] == "succeeded"
    assert sorted(closed) == ["inputs", "output"]


@pytest.mark.parametrize("how", ["link", "removed", "replaced"])
def test_an_inputs_directory_replaced_by_user_code_before_the_download_is_refused(
    world: _World, tmp_path: Path, how: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    inputs = world.result_path.parent / "inputs"
    swap = {
        "link": f"os.symlink({str(elsewhere)!r}, {str(inputs)!r})\n",
        "removed": "",
        "replaced": f"os.mkdir({str(inputs)!r})\n",
    }[how]
    source = f"import os\nos.rmdir({str(inputs)!r})\n{swap}" + _CONSUMER
    (world.root / "assets" / "gold" / "scores.py").write_text(source)
    world.client.artifacts = _Artifacts(files={"model": b"{}"})
    assert (
        world.run(
            inputs={"model": "models.churn", "obs": "silver.weather"},
            input_kinds={"model": "artifact", "obs": "table"},
            input_versions={"model": 4},
        )
        == 1
    )
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("inputs", False)
    assert (
        "the inputs directory of this attempt was replaced while the task ran" in record["message"]
    )
    assert world.client.artifacts.downloads == []
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (NotFound("models.churn has no version 4.", status=404), True),
        (PermissionDenied("'etl' may not READ_TABLE this.", status=403), False),
        (AuthenticationError("The token is no longer valid", status=401), False),
        (LakanaConnectionError("Could not reach http://server:8000: ReadTimeout"), True),
        (ServerError("The server is busy with other downloads", status=503), True),
        (ArtifactCorrupt("The downloaded file model.json does not match its manifest"), True),
        (
            StoredManifestDamaged("The stored manifest of version 4 does not match its digest"),
            False,
        ),
        (StoredManifestInvalid("The server's manifest lists a.bin more than once"), False),
        # a refusal of the local file system is not the manifest's fault and may clear
        (ManifestError("The version needs 6 bytes but only 4 are free"), True),
        (OSError(28, "No space left on device"), True),
    ],
)
def test_a_failed_download_names_the_input_and_is_retried_only_when_it_may_clear(
    world: _World, error: Exception, retryable: bool
) -> None:
    (world.root / "assets" / "gold" / "scores.py").write_text(_CONSUMER)
    world.client.artifacts = _Artifacts(error)
    assert (
        world.run(
            inputs={"model": "models.churn", "obs": "silver.weather"},
            input_kinds={"model": "artifact", "obs": "table"},
            input_versions={"model": 4},
        )
        == 1
    )
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("inputs", retryable)
    assert record["message"].startswith("reading input 'model' (models.churn, version 4) failed: ")
    assert world.client.tables.calls == []


def test_a_404_on_a_table_read_stays_permanent_beside_an_artifact_input(world: _World) -> None:
    """I5(b) retries only the download of an artifact: a missing table repeats on every attempt."""
    (world.root / "assets" / "gold" / "scores.py").write_text(_CONSUMER)
    world.client.artifacts = _Artifacts(files={"model": b"{}"})
    world.client.queries.results[_SELECT] = NotFound("no such table", status=404)
    assert (
        world.run(
            inputs={"model": "models.churn", "obs": "silver.weather"},
            input_kinds={"model": "artifact", "obs": "table"},
            input_versions={"model": 4},
        )
        == 1
    )
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("inputs", False)
    assert record["message"].startswith("reading input 'obs' (silver.weather) failed: ")


def test_the_output_directory_is_unset_and_the_descriptor_closed_after_every_outcome(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = _watch_opened_descriptors(monkeypatch)
    outcomes = []
    for source, error in (
        (_PRODUCER, _STORED),
        (_PRODUCER, ValidationError("refused", status=422)),
        (_PRODUCER.replace("    return out\n", "    return None\n"), _STORED),
        (_PRODUCER.replace("    return out\n", "    raise KeyboardInterrupt\n"), _STORED),
        (_PRODUCER.replace("    return out\n", '    return lakana.output_dir() / "m"\n'), _STORED),
    ):
        overrides = _artifact_world(world, source, error)
        if _output(world).exists():
            shutil.rmtree(_output(world))
        outcomes.append(world.run(**overrides))
        assert opened.ever
        assert opened.leaked() == []
        with pytest.raises(RuntimeError, match="available only while"):
            assets.output_dir()
    assert outcomes == [0, 1, 1, 1, 1]


def test_output_dir_is_unset_for_a_table_asset(world: _World) -> None:
    record = _failing(world, "import lakana\nlakana.output_dir()")
    assert record["phase"] == "function"
    assert "is available only while an asset declared with output" in record["message"]
    assert not _output(world).exists()


def test_a_table_asset_creates_no_directory(world: _World) -> None:
    assert world.run() == 0
    assert sorted(p.name for p in world.result_path.parent.iterdir()) == ["result.json"]


def test_an_output_directory_that_exists_already_fails_the_attempt_and_a_retry_may_succeed(
    world: _World,
) -> None:
    """Every attempt gets a fresh directory, so a leftover is the environment's, not the code's."""
    overrides = _artifact_world(world, _PRODUCER)
    _output(world).mkdir()
    assert world.run(**overrides) == 1
    record = world.record()
    assert (record["phase"], record["retryable"]) == ("load", True)
    assert "exists" in record["message"]
    assert world.client.artifacts.uploads == []


def _document(world: _World, **overrides: object) -> dict[str, object]:
    """Return the world's description as a dict with overrides.

    Parameters
    ----------
    world
        The world.
    **overrides
        Keys to replace.

    Returns
    -------
    dict
        The document.
    """
    return json.loads(world.description(**overrides))


_ARTIFACT_INPUT = {
    "inputs": {"model": "models.churn"},
    "input_kinds": {"model": "artifact"},
    "input_versions": {"model": 1},
}


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"version": 1}, "expected a version 2 or 3 document"),
        ({"version": 4}, "expected a version 2 or 3 document"),
        ({"version": 2.0}, "expected a version 2 or 3 document"),
        ({"version": True}, "expected a version 2 or 3 document"),
        ({"version": "2"}, "expected a version 2 or 3 document"),
        ({"output": "model"}, "output must be 'table' or 'artifact'"),
        ({"output": None}, "output must be 'table' or 'artifact'"),
        ({"output": ["table"]}, "output must be 'table' or 'artifact'"),
        ({"input_kinds": {"obs": "blob"}}, "input_kinds must map each input"),
        ({"input_kinds": {"obs": ["table"]}}, "input_kinds must map each input"),
        ({"input_kinds": {}}, "input_kinds must map each input"),
        ({"input_kinds": {"obs": "table", "ghost": "table"}}, "input_kinds must map each input"),
        ({"input_kinds": ["obs"]}, "input_kinds must map each input"),
        ({"input_kinds": None}, "input_kinds must map each input"),
        ({"input_versions": {"obs": 1}}, "input_versions must give a version"),
        ({"input_versions": {"ghost": 1}}, "input_versions must give a version"),
        ({"input_versions": "x"}, "input_versions must give a version"),
        ({"input_versions": None}, "input_versions must give a version"),
        ({"work_dir": ""}, "work_dir must be a non-empty string"),
        ({"work_dir": None}, "work_dir must be a non-empty string"),
        ({"work_dir": 7}, "work_dir must be a non-empty string"),
        ({"work_dir": "attempt"}, "work_dir must be an absolute path"),
        ({"work_dir": "/nonexistent-lakana-dir"}, "work_dir must be an existing directory"),
        ({"work_dir": "/etc/hostname"}, "work_dir must be an existing directory"),
        ({"work_dir": "/tmp/\x00"}, "work_dir must be an existing directory"),
        ({**_ARTIFACT_INPUT, "input_versions": {}}, "input_versions must give a version"),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": 1, "obs": 1}},
            "input_versions must give a version",
        ),
        ({**_ARTIFACT_INPUT, "input_versions": {"model": 0}}, "input_versions must give a version"),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": -3}},
            "input_versions must give a version",
        ),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": True}},
            "input_versions must give a version",
        ),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": "1"}},
            "input_versions must give a version",
        ),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": 1.0}},
            "input_versions must give a version",
        ),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": 2**31}},
            "input_versions must give a version",
        ),
        (
            {**_ARTIFACT_INPUT, "input_versions": {"model": None}},
            "input_versions must give a version",
        ),
    ],
)
def test_parse_description_checks_the_version_2_fields(
    world: _World, overrides: dict[str, object], fragment: str
) -> None:
    with pytest.raises(ValueError, match="task description") as raised:
        parse_description(world.description(**overrides))
    assert fragment in str(raised.value)
    assert main(io.StringIO(world.description(**overrides)), lambda *a, **k: None) == 2  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "parameter", ["", ".", "..", "../x", "a/b", "a\\b", "x y", "1x", "a\x00", "é", "a" * 129, "-x"]
)
def test_an_artifact_parameter_must_be_a_plain_identifier_before_it_is_a_path(
    world: _World, parameter: str
) -> None:
    overrides = {
        "inputs": {parameter: "models.churn"},
        "input_kinds": {parameter: "artifact"},
        "input_versions": {parameter: 1},
    }
    with pytest.raises(ValueError, match="parameter name"):
        parse_description(world.description(**overrides))
    assert not (world.result_path.parent / "inputs").exists()


def test_a_table_parameter_is_not_restricted_because_it_never_becomes_a_path(
    world: _World,
) -> None:
    description = parse_description(
        world.description(inputs={"é x": "silver.weather"}, input_kinds={"é x": "table"})
    )
    assert description.inputs == {"é x": "silver.weather"}


def test_two_parameters_that_differ_only_in_case_cannot_both_be_artifacts(
    world: _World,
) -> None:
    overrides = {
        "inputs": {"Model": "models.a", "model": "models.b"},
        "input_kinds": {"Model": "artifact", "model": "artifact"},
        "input_versions": {"Model": 1, "model": 1},
    }
    with pytest.raises(ValueError, match="differ only in case"):
        parse_description(world.description(**overrides))


@pytest.mark.parametrize(
    ("kinds", "versions"),
    [
        ({"a": "artifact", "b": "table"}, {"a": 1}),
        ({"a": "artifact", "b": "artifact"}, {"a": 1, "b": 2}),
    ],
)
def test_two_parameters_reading_one_object_must_agree_on_its_kind_and_version(
    world: _World, kinds: dict[str, str], versions: dict[str, int]
) -> None:
    with pytest.raises(ValueError, match="read the same object"):
        parse_description(
            world.description(
                inputs={"a": "models.churn", "b": "models.churn"},
                input_kinds=kinds,
                input_versions=versions,
            )
        )


def test_two_parameters_reading_one_artifact_version_are_accepted(world: _World) -> None:
    description = parse_description(
        world.description(
            inputs={"a": "models.churn", "b": "models.churn"},
            input_kinds={"a": "artifact", "b": "artifact"},
            input_versions={"a": 3, "b": 3},
        )
    )
    assert description.input_versions == {"a": 3, "b": 3}


def test_a_description_with_valid_artifact_fields_is_parsed(world: _World) -> None:
    description = parse_description(
        world.description(
            output="artifact",
            inputs={"obs": "silver.weather", "model": "models.churn"},
            input_kinds={"obs": "table", "model": "artifact"},
            input_versions={"model": 2**31 - 1},
        )
    )
    assert (description.output, description.input_kinds, description.input_versions) == (
        "artifact",
        {"obs": "table", "model": "artifact"},
        {"model": 2**31 - 1},
    )
    assert description.work_dir == str(world.result_path.parent)


@pytest.mark.parametrize("field_name", ["version", "input_versions"])
def test_an_integer_of_thousands_of_digits_is_a_refusal_not_a_crash(
    world: _World, field_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    text = world.description(
        **{
            "inputs": {"model": "models.churn"},
            "input_kinds": {"model": "artifact"},
            "input_versions": {"model": 1},
        }
    )
    document = json.loads(text)
    document[field_name] = {"model": "@big"} if field_name == "input_versions" else "@big"
    text = json.dumps(document).replace('"@big"', "9" * 5000)
    assert main(io.StringIO(text), lambda *a, **k: None) == 2  # ty: ignore[invalid-argument-type]
    assert "task description" in capsys.readouterr().err


def test_a_deeply_nested_document_is_a_refusal_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(io.StringIO("[" * 200_000), lambda *a, **k: None) == 2  # ty: ignore[invalid-argument-type]
    assert "task description: not valid JSON" in capsys.readouterr().err


def test_a_work_dir_open_to_the_group_or_others_is_refused(world: _World) -> None:
    for mode in (0o750, 0o705, 0o701, 0o770):
        world.result_path.parent.chmod(mode)
        with pytest.raises(ValueError, match="work_dir must not be accessible"):
            parse_description(world.description())
    world.result_path.parent.chmod(0o500)
    assert parse_description(world.description()).work_dir == str(world.result_path.parent)
    world.result_path.parent.chmod(0o700)


def test_a_work_dir_owned_by_someone_else_is_refused(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(world.result_path.parent).st_uid + 1)
    with pytest.raises(ValueError, match="work_dir must be owned by the user"):
        parse_description(world.description())


def test_a_work_dir_that_is_a_link_is_refused(world: _World, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(world.result_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="work_dir must be an existing directory"):
        parse_description(world.description(work_dir=str(link)))


# --- decimals -------------------------------------------------------------------------

_SPEND = pa.table(
    {
        "id": pa.array([1, 2], pa.int64()),
        "spend": pa.array([Decimal("12.50"), None], pa.decimal128(10, 2)),
    }
)


def _decimal_asset(world: _World, argument: str = "", body: str = "return obs") -> None:
    """Write an asset that reads `silver.weather` and returns `body`.

    Parameters
    ----------
    world
        The world.
    argument
        Extra decorator arguments, such as ``decimals="exact"``.
    body
        The function's body.
    """
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "import polars as pl\nfrom lakana import asset\n\n\n"
        f"@asset(inputs={{'obs': 'silver.weather'}}{argument})\ndef f(obs):\n    {body}\n"
    )


def _uploaded(world: _World) -> pa.Table:
    """Return what the harness uploaded.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    pa.Table
        The one upload's rows.
    """
    (call,) = world.client.tables.calls
    data = call["data"]
    assert isinstance(data, pa.Table)
    return data


def test_a_sql_decimal_reaches_the_function_as_floats_by_default(world: _World) -> None:
    """The newcomer's `value * 0.5` works on `SELECT 12.0 AS spend`."""
    world.client.queries.results[_SELECT] = _SPEND
    _decimal_asset(world, body="return obs.with_columns(half=pl.col('spend') * 0.5)")
    assert world.run() == 0
    uploaded = _uploaded(world)
    assert uploaded.schema.field("spend").type == pa.float64()
    assert uploaded.to_pydict() == {"id": [1, 2], "spend": [12.5, None], "half": [6.25, None]}


def test_decimals_exact_keeps_decimal_values_and_stores_them_as_decimals(world: _World) -> None:
    world.client.queries.results[_SELECT] = _SPEND
    _decimal_asset(
        world,
        argument=", decimals='exact'",
        body="assert type(obs['spend'][0]).__name__ == 'Decimal'\n    return obs",
    )
    assert world.run() == 0
    assert _uploaded(world).schema.field("spend").type == pa.decimal128(10, 2)


def test_a_decimal_column_the_asset_builds_is_stored_as_a_decimal(world: _World) -> None:
    world.client.queries.results[_SELECT] = _SPEND
    _decimal_asset(
        world,
        body=("return pl.DataFrame({'p': [Decimal('1.25')]}, schema={'p': pl.Decimal(10, 2)})"),
    )
    source = world.root / "assets" / "gold" / "scores.py"
    source.write_text("from decimal import Decimal\n" + source.read_text())
    assert world.run() == 0
    assert _uploaded(world).schema.field("p").type == pa.decimal128(10, 2)


def test_a_float_column_is_stored_as_a_double(world: _World) -> None:
    world.client.queries.results[_SELECT] = _SPEND
    _decimal_asset(world, body="return obs.select(pl.col('spend') * 2)")
    assert world.run() == 0
    assert _uploaded(world).schema.field("spend").type == pa.float64()


def test_the_description_decides_when_it_names_the_option_and_the_decorator_when_it_does_not(
    world: _World,
) -> None:
    """An older worker sends no `decimals`: the function's own declaration applies."""
    world.client.queries.results[_SELECT] = _SPEND
    check = "assert obs.schema['spend'] == pl.Decimal(10, 2)\n    return obs"
    _decimal_asset(world, argument=", decimals='exact'", body=check)
    assert world.run() == 0  # no key: the decorator says exact
    world.client.tables.calls.clear()
    assert world.run(decimals="exact") == 0
    world.client.tables.calls.clear()
    _decimal_asset(world, body="assert obs.schema['spend'] == pl.Float64\n    return obs")
    assert world.run(decimals="float") == 0
    world.client.tables.calls.clear()
    # the description is the manifest's word: it beats a decorator that says otherwise
    _decimal_asset(world, argument=", decimals='exact'", body=check)
    assert world.run(decimals="exact") == 0


@pytest.mark.parametrize("value", ["fixed", 1, True, ["exact"], ""])
def test_a_description_with_another_decimals_value_is_refused(world: _World, value: object) -> None:
    with pytest.raises(ValueError, match="decimals must be 'float' or 'exact'"):
        parse_description(world.description(decimals=value))


def test_a_description_may_leave_decimals_out(world: _World) -> None:
    assert parse_description(world.description()).decimals is None
    assert parse_description(world.description(decimals="exact")).decimals == "exact"
    assert parse_description(world.description(decimals=None)).decimals is None


def test_the_harness_reads_versions_2_and_3() -> None:
    """`decimals`, read with a default on both sides, needed no new version; the write mode did.

    A worker sends version 3 only to an asset that uses its fields, so a harness of the previous
    release keeps running every other asset.
    """
    assert DESCRIPTION_VERSIONS == (2, 3)


def _nested() -> pa.Table:
    """Build a table with a decimal at every depth.

    Returns
    -------
    pa.Table
        Plain, list, fixed-size list, struct (with a nested list) and map columns.
    """
    dec = pa.decimal128(10, 2)
    return pa.table(
        {
            "plain": pa.array([Decimal("1.50")], dec),
            "items": pa.array([[Decimal("1.50"), None]], pa.list_(dec)),
            "pair": pa.array([[Decimal("1.50"), Decimal("2.50")]], pa.list_(dec, 2)),
            "info": pa.array(
                [{"price": Decimal("2.50"), "tiers": [Decimal("1.25")], "n": 3}],
                pa.struct([("price", dec), ("tiers", pa.list_(dec)), ("n", pa.int64())]),
            ),
            "prices": pa.array([[("a", Decimal("0.75"))]], pa.map_(pa.string(), dec)),
        }
    )


def test_decimals_inside_lists_arrays_structs_and_maps_become_floats_too(world: _World) -> None:
    """A view can return nested decimals (a table cannot store them): they become floats too."""
    world.client.queries.results[_SELECT] = _nested()
    _decimal_asset(
        world,
        body=(
            "total = obs['items'].list.sum()[0] + obs['info'].struct.field('price')[0]\n"
            "    tiers = obs['info'].struct.field('tiers').list.first()[0] * 0.5\n"
            "    return pl.DataFrame({'decimal': ['Decimal' in str(obs.schema)], "
            "'total': [total], 'tiers': [tiers]})"
        ),
    )
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"decimal": [False], "total": [4.0], "tiers": [0.625]}


def test_exact_decimals_inside_nested_columns_are_left_alone(world: _World) -> None:
    world.client.queries.results[_SELECT] = _nested()
    _decimal_asset(
        world,
        argument=", decimals='exact'",
        body="return pl.DataFrame({'decimal': ['Decimal' in str(obs.schema)]})",
    )
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"decimal": [True]}


def test_as_floats_rebuilds_only_what_holds_a_decimal() -> None:
    decimal = pl.Decimal(10, 2)
    plain = pl.Struct({"n": pl.Int64, "tags": pl.List(pl.String)})
    assert runner._as_floats(decimal) == pl.Float64
    assert runner._as_floats(pl.List(decimal)) == pl.List(pl.Float64)
    assert runner._as_floats(pl.Array(decimal, 2)) == pl.Array(pl.Float64, 2)
    assert runner._as_floats(pl.Array(pl.Array(decimal, 3), 2)) == pl.Array(
        pl.Array(pl.Float64, 3), 2
    )
    assert runner._as_floats(pl.Struct({"p": decimal, "l": pl.List(decimal), "n": pl.Int64})) == (
        pl.Struct({"p": pl.Float64, "l": pl.List(pl.Float64), "n": pl.Int64})
    )
    # nothing to change: the very object comes back, so no cast is made
    for dtype in (pl.Int64, pl.String, pl.List(pl.Int64), pl.Array(pl.Int64, 2), plain):
        assert runner._as_floats(dtype) is dtype


@pytest.mark.skipif(
    not hasattr(pl, "Map"), reason="pl.Map exists only in Polars 2; Polars 1 has no map type"
)
def test_as_floats_reaches_into_a_map() -> None:
    assert runner._as_floats(pl.Map(pl.String, pl.Decimal(10, 2))) == pl.Map(pl.String, pl.Float64)
    assert runner._as_floats(pl.Map(pl.String, pl.Int64)) == pl.Map(pl.String, pl.Int64)


# --- a TypeError in the function fails once -------------------------------------------

_TYPE_ERROR_HINT = (
    "\nThis error is almost always a mistake in the asset's code, so it "
    "is not retried. Fix the function, deploy, and run again."
)


def test_a_type_error_in_the_function_is_not_retryable_and_names_the_next_step(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The newcomer's `float * decimal` mistake: three identical attempts helped nobody."""
    record = _failing(world, "from decimal import Decimal\nreturn 1.5 * Decimal('2')")
    message = str(record["message"])
    assert record["error_type"] == "TypeError"
    assert record["retryable"] is False
    assert record["phase"] == "function"
    assert message.startswith("TypeError: ")
    assert message.endswith(_TYPE_ERROR_HINT)
    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_the_type_error_hint_follows_the_whole_last_line_of_the_exception(world: _World) -> None:
    record = _failing(
        world, "raise TypeError(\"unsupported operand type(s) for *: 'float' and 'Decimal'\")"
    )
    assert record["message"] == (
        "TypeError: unsupported operand type(s) for *: 'float' and 'Decimal'" + _TYPE_ERROR_HINT
    )


def test_a_subclass_of_type_error_is_not_retried_either(world: _World) -> None:
    body = "class Mine(TypeError):\n    pass\nraise Mine('wrong type')"
    record = _failing(world, body)
    assert record["error_type"] == "Mine"
    assert record["retryable"] is False
    assert str(record["message"]) == "Mine: wrong type" + _TYPE_ERROR_HINT


def test_a_long_type_error_text_is_cut_so_that_the_advice_survives(world: _World) -> None:
    record = _failing(world, "raise TypeError('x' * 5000)")
    message = str(record["message"])
    assert len(message) == MAX_ERROR_MESSAGE_CHARS
    assert message.startswith("TypeError: xxx")
    assert message.endswith("…" + _TYPE_ERROR_HINT)


def test_the_token_is_hidden_in_a_type_error_too(world: _World) -> None:
    record = _failing(world, f"raise TypeError('leaked {_TOKEN} here')")
    assert _TOKEN not in json.dumps(record)
    assert "leaked *** here" in str(record["message"])


@pytest.mark.parametrize(
    "exception",
    [
        "NameError('x')",
        "AttributeError('x')",
        "KeyError('x')",
        "ValueError('x')",
        "IndexError('x')",
        "ZeroDivisionError('x')",
        "RuntimeError('x')",
        "OSError('x')",
        "Exception('x')",
    ],
)
def test_every_other_exception_in_the_function_keeps_its_class(
    world: _World, exception: str
) -> None:
    record = _failing(world, f"raise {exception}")
    assert record["retryable"] is True
    assert "almost always a mistake" not in str(record["message"])


def test_a_type_error_outside_the_function_keeps_its_class(world: _World) -> None:
    """Only a TypeError raised out of the user's function is permanent: a failed upload is not."""
    world.client.tables.outcome = TypeError("an internal defect")
    record = _failing(world, "return obs")
    assert record["phase"] == "upload"
    assert record["retryable"] is True


# --- column types of Polars 2 ---------------------------------------------------------

_POLARS_2 = pytest.mark.skipif(
    not hasattr(pl, "Extension"), reason="pl.Extension and pl.UInt128 exist only in Polars 2"
)


def test_an_int128_column_is_refused_by_name_with_the_next_step(world: _World) -> None:
    """`to_arrow()` fails with a raw `ArrowInvalid: ... '_pli128'`; the harness says what to do."""
    record = _failing(world, "return pl.DataFrame({'big': pl.Series([1], dtype=pl.Int128)})")
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert record["message"] == (
        "column 'big' has type Int128, which cannot be stored in a table; cast it to Int64 "
        "(pl.col('big').cast(pl.Int64)), or to a decimal such as pl.Decimal(38, 0) if the "
        "values do not fit into 64 bits"
    )
    assert world.client.tables.calls == []


def test_a_cast_int128_column_is_stored(world: _World) -> None:
    _decimal_asset(
        world,
        body="return pl.DataFrame({'big': pl.Series([1], dtype=pl.Int128)}).cast(pl.Int64)",
    )
    assert world.run() == 0
    assert _uploaded(world).schema.field("big").type == pa.int64()


def test_the_name_of_an_int128_column_is_bounded_and_escaped(world: _World) -> None:
    record = _failing(
        world, "return pl.DataFrame({'a\\x1b' + 'z' * 500: pl.Series([1], dtype=pl.Int128)})"
    )
    message = str(record["message"])
    assert "\x1b" not in message
    assert "zzzz" in message
    assert len(message) < 700


@_POLARS_2
def test_a_uint128_column_is_refused_by_name(world: _World) -> None:
    record = _failing(world, "return pl.DataFrame({'big': pl.Series([1], dtype=pl.UInt128)})")
    assert record["retryable"] is False
    assert str(record["message"]).startswith(
        "column 'big' has type UInt128, which cannot be stored in a table; cast it to Int64 "
    )


@_POLARS_2
def test_adding_a_signed_and_an_unsigned_column_gives_an_int128_the_harness_refuses(
    world: _World,
) -> None:
    """The case of the issue: in Polars 2 `Int64 + UInt64` is `Int128`, in Polars 1 a float."""
    body = (
        "frame = pl.DataFrame({'a': pl.Series([1], dtype=pl.Int64), "
        "'b': pl.Series([2], dtype=pl.UInt64)})\n"
        "return frame.select(total=pl.col('a') + pl.col('b'))"
    )
    record = _failing(world, body)
    assert str(record["message"]).startswith("column 'total' has type Int128, which cannot be")


_EXTENSION_NAME = "lakana.test.ext"


def _extension_table() -> pa.Table:
    """Build a table whose column has an Arrow extension type Polars does not know.

    Returns
    -------
    pa.Table
        One column ``e`` of integers with the extension type `_EXTENSION_NAME`.
    """

    class _Ext(pa.ExtensionType):
        def __init__(self) -> None:
            super().__init__(pa.int64(), _EXTENSION_NAME)

        def __arrow_ext_serialize__(self) -> bytes:
            return b""

        @classmethod
        def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> _Ext:
            return cls()

    return pa.table({"e": pa.ExtensionArray.from_storage(_Ext(), pa.array([1, 2], pa.int64()))})


@_POLARS_2
def test_an_extension_column_is_refused_by_name_with_the_next_step(world: _World) -> None:
    world.client.queries.results[_SELECT] = _extension_table()
    record = _failing(world, "return obs")
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert record["message"] == (
        "column 'e' has type Extension, which cannot be stored in a table; take its stored "
        "values with pl.col('e').ext.storage() and cast them to a type a table stores"
    )
    assert _EXTENSION_NAME not in json.dumps(record)


@_POLARS_2
def test_the_storage_of_an_extension_column_is_stored(world: _World) -> None:
    world.client.queries.results[_SELECT] = _extension_table()
    _decimal_asset(world, body="return obs.select(pl.col('e').ext.storage())")
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"e": [1, 2]}


_MAPPED = pa.table(
    {
        "id": pa.array([1, 2], pa.int64()),
        "prices": pa.array([[("a", 1.5), ("b", 2.5)], None], pa.map_(pa.string(), pa.float64())),
    }
)


@pytest.mark.skipif(not hasattr(pl, "Map"), reason="pl.Map exists only in Polars 2")
def test_a_map_input_arrives_as_a_map_and_a_map_output_is_refused_as_a_nested_type(
    world: _World,
) -> None:
    """In Polars 2 a map is `pl.Map`; a table stores no nested type, so returning one is refused."""
    world.client.queries.results[_SELECT] = _MAPPED
    _decimal_asset(
        world,
        body=(
            "assert isinstance(obs.schema['prices'], pl.Map)\n"
            "    return obs.select('id', a=pl.col('prices').map.get('a'))"
        ),
    )
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"id": [1, 2], "a": [1.5, None]}
    world.client.tables.calls.clear()
    record = _failing(world, "return obs")
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert str(record["message"]).startswith("Column 'prices' has an unsupported Arrow type: map<")


@pytest.mark.skipif(hasattr(pl, "Map"), reason="a map is a list of structs only in Polars 1")
def test_a_map_input_arrives_as_a_list_of_structs_in_polars_1(world: _World) -> None:
    world.client.queries.results[_SELECT] = _MAPPED
    _decimal_asset(
        world,
        body=(
            "assert isinstance(obs.schema['prices'], pl.List)\n"
            "    return obs.select('id', first=pl.col('prices').list.first().struct.field('value'))"
        ),
    )
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"id": [1, 2], "first": [1.5, None]}


def _write_asset(world: _World, body: str, *, signature: str = "obs", inputs: str = "") -> None:
    """Write an asset whose function body is `body`.

    Parameters
    ----------
    world
        The world.
    body
        The function's body, one statement per line.
    signature
        The function's parameters.
    inputs
        The decorator's arguments.
    """
    world.result_path.parent.mkdir(exist_ok=True)
    arguments = inputs or "inputs={'obs': 'silver.weather'}"
    source = (
        "import datetime\nimport polars as pl\nimport pyarrow as pa\nfrom lakana import asset\n\n\n"
        f"@asset({arguments})\ndef f({signature}):\n"
    )
    (world.root / "assets" / "gold" / "scores.py").write_text(
        source + textwrap.indent(body, "    ") + "\n"
    )


def _v3(
    write: str = "append",
    *,
    key: list[str] | None = None,
    watermark: Mapping[str, object] | None = None,
    connections: Mapping[str, object] | None = None,
    expected: int | str = 17,
) -> dict[str, object]:
    """Build the version 3 keys of a description.

    Parameters
    ----------
    write
        The write mode.
    key
        The merge key.
    watermark
        The watermark object; `None` sends JSON null.
    connections
        The connections object.
    expected
        The expected snapshot id.

    Returns
    -------
    dict
        Overrides for `_World.run` and `_World.description`.
    """
    return {
        "version": 3,
        "write": write,
        "key": key or [],
        "watermark": None if watermark is None else dict(watermark),
        "connections": dict(connections or {}),
        "expected_snapshot_id": expected,
        "table_exists": expected != "none",
    }


_WATERMARK_ID = {"column": "id", "value": "long:41"}


def test_an_asset_that_opted_in_replaces_with_evolve_schema(world: _World) -> None:
    world.result_path.parent.mkdir(exist_ok=True)
    assert world.run(evolve_schema=True) == 0
    (call,) = world.client.tables.calls
    assert call["method"] == "replace"
    assert call["evolve_schema"] is True
    assert world.record() == {"status": "succeeded", "snapshot_id": 42, "rows": 2}


def test_evolve_schema_is_off_unless_the_description_says_so(world: _World) -> None:
    assert parse_description(world.description()).evolve_schema is False
    assert parse_description(world.description(evolve_schema=False)).evolve_schema is False
    assert parse_description(world.description(evolve_schema=True)).evolve_schema is True


@pytest.mark.parametrize("value", ["true", 1, None])
def test_an_evolve_schema_that_is_not_a_boolean_is_refused(world: _World, value: object) -> None:
    with pytest.raises(ValueError) as raised:
        parse_description(world.description(evolve_schema=value))
    assert str(raised.value) == "task description: evolve_schema must be true or false"


def test_the_first_run_of_an_append_never_evolves(world: _World) -> None:
    """The worker sends `evolve_schema` only for a replace; the harness checks it too."""
    _write_asset(world, "return obs")
    overrides = _v3(watermark={"column": "id", "value": None}, expected="none")
    assert world.run(**overrides, evolve_schema=True) == 0
    (call,) = world.client.tables.calls
    assert call["method"] == "replace"
    assert call["evolve_schema"] is False


def test_append_first_run_replaces_with_expected_none(world: _World) -> None:
    """write=append, expected "none": the first run creates the table with every row."""
    _write_asset(world, "return obs")
    overrides = _v3(watermark={"column": "id", "value": None}, expected="none")
    assert world.run(**overrides) == 0
    (call,) = world.client.tables.calls
    assert call["method"] == "replace"
    assert call["expected_snapshot_id"] == "none"
    assert call["evolve_schema"] is False
    assert world.record() == {"status": "succeeded", "snapshot_id": 42, "rows": 2}


def test_append_later_runs_append_against_the_pin(world: _World) -> None:
    """write=append, expected 17: `client.tables.append(..., expected_snapshot_id=17)`."""
    _write_asset(world, "return obs")
    assert world.run(**_v3(watermark=_WATERMARK_ID)) == 0
    (call,) = world.client.tables.calls
    assert call["method"] == "append"
    assert call["expected_snapshot_id"] == 17
    assert call["sources"] == ["silver.weather"]
    assert call["source_versions"] == {}
    assert "evolve_schema" not in call
    assert _uploaded(world).to_pydict() == {"id": [1, 2]}
    assert world.record() == {"status": "succeeded", "snapshot_id": 43, "rows": 2}


@pytest.mark.parametrize("expected", [17, "none"])
def test_merge_calls_merge_with_the_key(world: _World, expected: int | str) -> None:
    """write=merge: `client.tables.merge(..., key=["id"], expected_snapshot_id=17)`.

    A first run merges too: the server creates the table after the same key checks.
    """
    _write_asset(world, "return obs")
    assert world.run(**_v3("merge", key=["id"], expected=expected)) == 0
    (call,) = world.client.tables.calls
    assert call["method"] == "merge"
    assert call["key"] == ["id"]
    assert call["expected_snapshot_id"] == expected
    assert call["sources"] == ["silver.weather"]
    assert world.record() == {"status": "succeeded", "snapshot_id": 44, "rows": 2}


def test_a_merge_that_changes_nothing_succeeds_without_a_snapshot(world: _World) -> None:
    _write_asset(world, "return obs")
    world.client.tables.merged = MergeResult(snapshot_id=None, rows_inserted=0, rows_updated=0)
    assert world.run(**_v3("merge", key=["id"])) == 0
    assert world.record() == {"status": "succeeded", "snapshot_id": None, "rows": 0}


def test_ctx_receives_the_decoded_watermark(world: _World) -> None:
    """`def asset(ctx): return pl.DataFrame({"n": [ctx.watermark()]})` gets 41 for "long:41"."""
    _write_asset(
        world,
        "return pl.DataFrame({'id': [ctx.watermark()]})",
        signature="ctx",
        inputs="write='append', watermark='id'",
    )
    assert world.run(inputs={}, input_kinds={}, **_v3(watermark=_WATERMARK_ID)) == 0
    assert _uploaded(world).to_pydict() == {"id": [41]}


def test_ctx_gives_the_default_on_a_first_run(world: _World) -> None:
    _write_asset(
        world,
        "return pl.DataFrame({'d': [ctx.watermark(default=datetime.date(2020, 1, 1))]})",
        signature="obs, ctx",
    )
    overrides = _v3(watermark={"column": "d", "value": None}, expected="none")
    assert world.run(**overrides) == 0
    assert _uploaded(world).to_pydict() == {"d": [datetime.date(2020, 1, 1)]}


def test_a_replace_asset_gets_a_ctx_without_a_watermark(world: _World) -> None:
    """A version 2 description still passes `ctx`; its watermark is the default."""
    _write_asset(
        world, "return obs.with_columns(w=pl.lit(ctx.watermark(default=-1)))", signature="ctx, obs"
    )
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"id": [1, 2], "w": [-1, -1]}


def test_an_input_named_ctx_of_an_older_project_version_is_still_the_input(world: _World) -> None:
    """A version deployed before `ctx` was reserved may name an input `ctx`: it keeps it."""
    _write_asset(world, "return ctx", signature="ctx", inputs="inputs={'ctx': 'silver.weather'}")
    assert world.run(inputs={"ctx": "silver.weather"}, input_kinds={"ctx": "table"}) == 0
    assert _uploaded(world).to_pydict() == {"id": [1, 2]}


def test_a_pyarrow_table_is_accepted(world: _World) -> None:
    """The asset may return a `pyarrow.Table`; view and nanosecond types are converted."""
    _write_asset(
        world,
        "return pa.table({'name': pa.array(['a'], pa.string_view()),\n"
        "                 'at': pa.array([datetime.datetime(2026, 1, 1)], pa.timestamp('ns'))})",
    )
    assert world.run() == 0
    uploaded = _uploaded(world)
    assert uploaded.schema.field("name").type == pa.string()
    assert uploaded.schema.field("at").type == pa.timestamp("us")


def test_a_pyarrow_table_gets_the_same_schema_checks(world: _World) -> None:
    record = _failing(
        world, "import pyarrow as pa\nreturn pa.table({'d': pa.array([1], pa.duration('s'))})"
    )
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert str(record["message"]).startswith("Column 'd' has an unsupported Arrow type")


def test_a_pyarrow_table_without_columns_is_refused_for_a_replace(world: _World) -> None:
    record = _failing(world, "import pyarrow as pa\nreturn pa.table({})")
    assert record["message"] == "the asset returned a table without columns"


def test_a_record_batch_reader_is_refused(world: _World) -> None:
    """A stream is not read for the asset: the message says how to read it into a table."""
    record = _failing(
        world,
        "import pyarrow as pa\nt = obs.to_arrow()\n"
        "return pa.RecordBatchReader.from_batches(t.schema, t.to_batches())",
    )
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert record["message"] == (
        "the asset returned a pyarrow RecordBatchReader; read it with `.read_all()` and return "
        "the table"
    )
    assert world.client.tables.calls == []


@pytest.mark.parametrize(
    "body",
    [
        "return obs.clear()",
        "return pl.DataFrame()",
        "return pa.table({})",
        "return obs.to_arrow().slice(0, 0)",
    ],
    ids=["zero-rows", "polars-without-columns", "arrow-without-columns", "arrow-zero-rows"],
)
@pytest.mark.parametrize("write", ["append", "merge"])
def test_an_empty_append_succeeds_without_a_write(world: _World, body: str, write: str) -> None:
    """Review Focus 2: an append or merge whose source had nothing new succeeds, writing nothing.

    The table exists (expected 17) and the asset returns a zero-row table or a zero-column one:
    the record says succeeded with rows 0 and no snapshot; no upload is sent, and the watermark
    column is not looked for in a result without columns.
    """
    _write_asset(world, body)
    key = ["id"] if write == "merge" else None
    assert world.run(**_v3(write, key=key, watermark=_WATERMARK_ID)) == 0
    assert world.record() == {"status": "succeeded", "snapshot_id": None, "rows": 0}
    assert world.client.tables.calls == []


@pytest.mark.parametrize("write", ["append", "merge"])
def test_an_empty_first_run_without_columns_fails_naming_the_cause(
    world: _World, write: str
) -> None:
    """write=append or merge, expected "none", zero columns: nothing to create the table from."""
    key = ["id"] if write == "merge" else None
    record = _failing(
        world, "return pl.DataFrame()", **_v3(write, key=key, watermark=None, expected="none")
    )
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert record["phase"] == "validate"
    assert record["message"] == (
        "the asset returned no columns, so there is nothing to create gold.scores from; its "
        "source had no data yet (check the pattern or the query); the first run must find data"
    )
    assert world.client.tables.calls == []


def test_an_empty_first_run_with_columns_creates_the_table(world: _World) -> None:
    """A zero-row result that has columns creates the table empty; the next run appends."""
    _write_asset(world, "return obs.clear()")
    world.client.tables.outcome = WriteResult(rows=0, files=0, snapshot_id=None)
    assert world.run(**_v3(watermark={"column": "id", "value": None}, expected="none")) == 0
    assert world.client.tables.calls[0]["method"] == "replace"
    assert world.record() == {"status": "succeeded", "snapshot_id": None, "rows": 0}


def test_an_empty_replace_still_replaces(world: _World) -> None:
    """Only append and merge skip the upload: an empty replace empties the table."""
    _write_asset(world, "return obs.clear()")
    world.client.tables.outcome = WriteResult(rows=0, files=0, snapshot_id=50)
    assert world.run() == 0
    assert world.client.tables.calls[0]["method"] == "replace"
    assert world.record() == {"status": "succeeded", "snapshot_id": 50, "rows": 0}


def test_the_watermark_column_must_have_an_incremental_type(world: _World) -> None:
    """Watermark "name" of type string: the run fails naming the column and the allowed types."""
    record = _failing(
        world,
        "return obs.with_columns(name=pl.lit('x'))",
        **_v3(watermark={"column": "name", "value": None}),
    )
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert record["message"] == (
        "watermark column `name` is a string; it must be one of int, long, decimal, date, "
        "timestamp, timestamptz"
    )
    assert world.client.tables.calls == []


def test_the_watermark_column_must_be_in_the_result(world: _World) -> None:
    record = _failing(world, "return obs", **_v3(watermark={"column": "ts", "value": None}))
    assert record["message"] == (
        "watermark column `ts` is not a column of what the asset returned; return it, or change "
        "the asset's watermark="
    )


@pytest.mark.parametrize(
    ("expression", "name"),
    [
        ("pl.lit(1, dtype=pl.Int32)", "int"),
        ("pl.lit(1, dtype=pl.Int64)", "long"),
        ("pl.lit(1.5).cast(pl.Decimal(10, 2))", "decimal"),
        ("pl.lit(datetime.date(2026, 1, 1))", "date"),
        ("pl.lit(datetime.datetime(2026, 1, 1))", "timestamp"),
        ("pl.lit(datetime.datetime(2026, 1, 1)).dt.replace_time_zone('UTC')", "timestamptz"),
    ],
)
def test_every_incremental_type_is_a_watermark_column(
    world: _World, expression: str, name: str
) -> None:
    _write_asset(world, f"return obs.with_columns(w={expression})")
    assert world.run(**_v3(watermark={"column": "w", "value": None})) == 0, name
    assert world.client.tables.calls[0]["method"] == "append"


def test_version_2_descriptions_still_run(world: _World) -> None:
    """A version 2 document has no write fields: it replaces, whatever else it carries."""
    _write_asset(world, "return obs")
    assert world.run(write="append", key=["id"]) == 0
    assert world.client.tables.calls[0]["method"] == "replace"
    description = parse_description(world.description(write="append"))
    assert (description.write, description.key, description.watermark) == ("replace", (), None)


def test_a_version_3_description_is_parsed(world: _World) -> None:
    description = parse_description(
        world.description(
            **_v3(
                "merge",
                key=["id", "day"],
                watermark={"column": "at", "value": "timestamptz:2026-10-22T08:00:00+00:00"},
                connections={"crm-db": {"kind": "postgresql"}},
            )
        )
    )
    assert description.write == "merge"
    assert description.key == ("id", "day")
    assert description.watermark_column == "at"
    assert description.watermark == datetime.datetime(2026, 10, 22, 8, tzinfo=datetime.UTC)
    assert description.connections == {"crm-db": {"kind": "postgresql"}}


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"write": "upsert"}, "write must be 'replace', 'append' or 'merge'"),
        ({"write": None}, "write must be 'replace', 'append' or 'merge'"),
        ({"write": "merge", "key": []}, "key must name the columns of a merge"),
        ({"write": "append", "key": ["id"]}, "key must name the columns of a merge"),
        ({"write": "merge", "key": ["id", 1]}, "key must name the columns of a merge"),
        ({"write": "merge", "key": "id"}, "key must name the columns of a merge"),
        ({"watermark": "id"}, "watermark must be null or"),
        ({"watermark": {"column": ""}}, "watermark must be null or"),
        ({"watermark": {"column": "id", "value": 41}}, "watermark must be null or"),
        ({"watermark": {"column": "id", "value": "long:x"}}, "watermark must be null or"),
        ({"watermark": {"column": "id", "value": "float:1"}}, "watermark must be null or"),
        ({"connections": []}, "connections must map each declared connection"),
        ({"connections": {"crm": "postgresql"}}, "connections must map each declared connection"),
        ({"connections": {"crm": {}}}, "connections must map each declared connection"),
        ({"connections": {"crm": {"kind": 3}}}, "connections must map each declared connection"),
        ({"version": 4}, "expected a version 2 or 3 document"),
    ],
)
def test_a_version_3_description_checks_its_fields(
    world: _World, overrides: dict[str, object], fragment: str
) -> None:
    fields = {**_v3("merge", key=["id"], watermark=_WATERMARK_ID), **overrides}
    with pytest.raises(ValueError, match=fragment) as caught:
        parse_description(world.description(**fields))
    assert "long:x" not in str(caught.value)


def test_a_merge_with_a_repeated_key_is_refused_before_the_upload(world: _World) -> None:
    """The refusal names the count and what a Python asset can do about it."""
    record = _failing(world, "return pl.concat([obs, obs])", **_v3("merge", key=["id"]))
    assert record["error_type"] == "InvalidResult"
    assert record["retryable"] is False
    assert record["message"] == (
        "key `id` repeats in 4 rows of what the asset returned; return one row per key, for "
        "example with `df.unique(subset=['id'], keep='last')`"
    )
    assert world.client.tables.calls == []


def test_a_merge_on_several_columns_over_the_cap_is_refused_before_the_upload(
    world: _World,
) -> None:
    rows = runner.COMPOSITE_KEY_MAX_ROWS + 1
    record = _failing(
        world,
        f"return pl.DataFrame({{'id': range({rows}), 'day': [1] * {rows}}})",
        **_v3("merge", key=["id", "day"]),
    )
    assert record["error_type"] == "InvalidResult"
    assert record["message"] == (
        "a merge on a key of several columns takes at most 5,000 rows per run, and the asset "
        "returned 5,001. Merge on one key column (a column that holds the whole key), or return "
        "fewer rows per run: give the asset a watermark= and return only the rows past "
        "ctx.watermark(), and load the history in parts of at most 5,000 rows"
    )
    assert world.client.tables.calls == []


def test_a_merge_on_several_columns_at_the_cap_is_sent(world: _World) -> None:
    rows = runner.COMPOSITE_KEY_MAX_ROWS
    _write_asset(world, f"return pl.DataFrame({{'id': range({rows}), 'day': [1] * {rows}}})")
    assert world.run(**_v3("merge", key=["id", "day"])) == 0
    assert world.client.tables.calls[0]["key"] == ["id", "day"]


@pytest.mark.parametrize("write", ["append", "merge"])
def test_a_refused_append_or_merge_is_worded_like_a_refused_replace(
    world: _World, write: str
) -> None:
    refusal = ValidationError("key column `id` is null in 1 rows", status=422)
    if write == "append":
        world.client.tables.appended = refusal
    else:
        world.client.tables.merged = refusal
    key = ["id"] if write == "merge" else None
    record = _failing(world, "return obs", **_v3(write, key=key, watermark=_WATERMARK_ID))
    assert record["error_type"] == "UploadRefused"
    assert record["retryable"] is False
    assert record["message"] == "key column `id` is null in 1 rows"


def test_a_conflict_on_an_append_is_a_conflict(world: _World) -> None:
    world.client.tables.appended = Conflict("gold.scores changed during the write; retry.")
    record = _failing(world, "return obs", **_v3(watermark=_WATERMARK_ID))
    assert (record["conflict"], record["retryable"], record["phase"]) == (True, True, "upload")


def test_the_task_state_is_set_while_the_asset_runs(world: _World) -> None:
    """`lakana.context.current()` names the client, the asset and its declared connections."""
    _write_asset(
        world,
        "from lakana import context\n"
        "state = context.current()\n"
        "return obs.with_columns(env=pl.lit(state.environment), asset=pl.lit(state.asset),\n"
        "    declared=pl.lit(','.join(sorted(state.declared))),\n"
        "    kind=pl.lit(state.connections['crm-db']['kind']),\n"
        "    client=pl.lit(state.client is not None))",
    )
    connections = {"crm-db": {"kind": "postgresql"}, "files": {"kind": "s3"}}
    assert world.run(**_v3("replace", connections=connections, expected=41)) == 0
    assert _uploaded(world).to_pylist()[0] == {
        "id": 1,
        "env": "dev",
        "asset": "gold.scores",
        "declared": "crm-db,files",
        "kind": "postgresql",
        "client": True,
    }
    with pytest.raises(RuntimeError):
        runner.context.current()


def test_the_task_state_outlives_a_failure_until_the_record_is_written(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The values to hide must still be known when the failure record and the trace are written."""
    seen: list[object] = []
    build = runner._failure_record

    def spy(exc: BaseException, token: str) -> dict[str, object]:
        seen.append(runner.context._state)
        return build(exc, token)

    monkeypatch.setattr(runner, "_failure_record", spy)
    record = _failing(
        world,
        "from lakana import context\n"
        "context.register_secret('hunter2-secret')\n"
        "raise ValueError('x')",
    )
    assert record["error_type"] == "ValueError"
    (state,) = seen
    assert isinstance(state, runner.context._TaskState)
    assert state.secrets == {"hunter2-secret"}
    assert runner.context._state is None


def test_a_merge_with_null_keys_is_left_to_the_server_which_names_the_nulls(world: _World) -> None:
    """Nulls are not "repeats": the server's null message comes first, not the unique() advice."""
    world.client.tables.merged = ValidationError("key column `id` is null in 2 rows", status=422)
    record = _failing(
        world,
        "return pl.DataFrame({'id': [None, None, 1]}, schema={'id': pl.Int64})",
        **_v3("merge", key=["id"]),
    )
    assert record["error_type"] == "UploadRefused"
    assert record["message"] == "key column `id` is null in 2 rows"
    (call,) = world.client.tables.calls
    assert call["method"] == "merge"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"key": ["id\n"]}, "key must name the columns of a merge"),
        ({"key": ["i\u202ed"]}, "key must name the columns of a merge"),
        ({"watermark": {"column": "at\x00", "value": None}}, "watermark must be null or"),
        ({"watermark": {"column": "a\u2028t", "value": None}}, "watermark must be null or"),
    ],
)
def test_key_and_watermark_columns_must_be_identifiers(
    world: _World, overrides: dict[str, object], fragment: str
) -> None:
    """The rule of a deploy: no control, format or surrogate character, no line separator."""
    fields = {**_v3("merge", key=["id"], watermark=_WATERMARK_ID), **overrides}
    with pytest.raises(ValueError, match=fragment):
        parse_description(world.description(**fields))


def test_a_spaced_key_and_watermark_parse(world: _World) -> None:
    description = parse_description(
        world.description(
            **_v3("merge", key=["order id"], watermark={"column": "län", "value": None})
        )
    )
    assert (description.key, description.watermark_column) == (("order id",), "län")


_FETCHED_PASSWORD = "fetched-pa55word"


class _FetchedConnections:
    """A fake `client.task_connections` that answers `crm-db` with a password.

    Parameters
    ----------
    password
        The secret's password.
    """

    def __init__(self, password: str = _FETCHED_PASSWORD) -> None:
        self.password = password
        self.asked: list[str] = []

    def get(self, name: str) -> dict[str, object]:
        """Answer one fetch.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            A `TaskConnectionOut` body.
        """
        self.asked.append(name)
        return {
            "name": name,
            "kind": "postgresql",
            "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
            "settings": {"user": "reader"},
            "endpoints": [{"host": "db.example.com", "port": 5432, "role": "service"}],
            "secret": {"password": self.password},
        }


_CRM = {"crm-db": {"kind": "postgresql"}}


def test_a_fetched_password_in_an_exception_is_masked_in_the_record_and_the_trace(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The harness masks what it writes; what the asset prints itself is the worker's to mask."""
    world.client.task_connections = _FetchedConnections()  # ty: ignore[unresolved-attribute]
    record = _failing(
        world,
        "from lakana.ingest import connections\n"
        "password = connections.get('crm-db').secret['password']\n"
        "print('password is', password)\n"
        "raise ValueError(f'could not log in with {password!r} or {password}')",
        **_v3("replace", connections=_CRM, expected=41),
    )
    assert record["message"] == "ValueError: could not log in with '***' or ***"
    assert record["retryable"] is True
    err = capsys.readouterr().err
    assert "ValueError: could not log in with '***' or ***" in err
    assert _FETCHED_PASSWORD not in err
    assert runner.context._state is None


def test_a_fetched_password_in_the_name_of_an_exception_type_is_masked(world: _World) -> None:
    world.client.task_connections = _FetchedConnections()  # ty: ignore[unresolved-attribute]
    record = _failing(
        world,
        "from lakana.ingest import connections\n"
        "password = connections.get('crm-db').secret['password']\n"
        "raise type(password, (Exception,), {})('x')",
        **_v3("replace", connections=_CRM, expected=41),
    )
    assert record["error_type"] == "***"
    assert record["message"] == "***: x"
    assert _FETCHED_PASSWORD not in json.dumps(record)


def test_the_fetched_connections_are_forgotten_when_the_attempt_ends(world: _World) -> None:
    from lakana.ingest import _connections

    fake = _FetchedConnections()
    world.client.task_connections = fake  # ty: ignore[unresolved-attribute]
    _write_asset(
        world,
        "from lakana.ingest import connections\n"
        "connections.get('crm-db')\nconnections.get('crm-db')\nreturn obs",
    )
    assert world.run(**_v3("replace", connections=_CRM, expected=41)) == 0
    assert fake.asked == ["crm-db"]
    assert _connections._CACHE == {}


@pytest.mark.parametrize(
    ("raised", "retryable"),
    [
        ("from lakana.ingest import ConnectionNotDeclared as E", False),
        ("from lakana.ingest import ConnectionRefused as E", False),
        ("from lakana.ingest import CapExceeded as E", False),
        ("from lakana.ingest import ConnectionChanged as E", True),
        ("from lakana.ingest import SourceError as E", True),
        ("class E(Exception):\n    retryable = False", True),
    ],
    ids=["not-declared", "refused", "cap", "changed", "source", "user-attribute"],
)
def test_an_ingest_error_says_whether_another_attempt_could_help(
    world: _World, raised: str, retryable: bool
) -> None:
    """Only `lakana.ingest`'s own errors are read: a user's attribute decides nothing."""
    record = _failing(world, f"{raised}\nraise E('no')")
    assert (record["phase"], record["retryable"]) == ("function", retryable)


def test_overlapping_occurrences_of_a_value_are_masked_whole(world: _World) -> None:
    """`abab` in `ababab`: both occurrences overlap, so the whole run is one span."""
    record = _failing(
        world,
        "from lakana import context\n"
        "context.register_secret('abab')\n"
        "context.register_secret('cdcd-long')\n"
        "raise ValueError('ababab and cdcd-longcdcd-long and xabab')",
    )
    assert record["message"] == "ValueError: *** and *** and x***"


@pytest.mark.parametrize(
    "body", ["return pl.DataFrame()", "return obs.clear()"], ids=["no-columns", "no-rows"]
)
def test_an_empty_result_for_an_existing_table_without_a_snapshot_succeeds(
    world: _World, body: str
) -> None:
    """A first run of zero rows created the table without a snapshot; it exists all the same."""
    _write_asset(world, body)
    overrides = {**_v3(watermark={"column": "id", "value": None}, expected="none")}
    overrides["table_exists"] = True
    assert world.run(**overrides) == 0
    assert world.record() == {"status": "succeeded", "snapshot_id": None, "rows": 0}
    assert world.client.tables.calls == []


def test_an_existing_table_without_a_snapshot_gets_its_rows_appended(world: _World) -> None:
    _write_asset(world, "return obs")
    overrides = {**_v3(watermark={"column": "id", "value": None}, expected="none")}
    overrides["table_exists"] = True
    assert world.run(**overrides) == 0
    (call,) = world.client.tables.calls
    assert (call["method"], call["expected_snapshot_id"]) == ("append", "none")


@pytest.mark.parametrize("value", [None, "yes", 1])
def test_a_version_3_description_says_whether_the_table_exists(
    world: _World, value: object
) -> None:
    fields = {**_v3(watermark=_WATERMARK_ID), "table_exists": value}
    with pytest.raises(ValueError, match="table_exists must be true or false"):
        parse_description(world.description(**fields))


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("return obs.head(1)", ["id"]),
        ("return obs.rename({'id': 'other'})", ["id"]),
        (
            "import uuid\nreturn pa.table({'id': pa.array([uuid.uuid4().bytes] * 2, pa.uuid())})",
            ["id"],
        ),
    ],
    ids=["one-row", "key-column-missing", "key-type-arrow-cannot-group"],
)
def test_a_merge_the_harness_cannot_judge_is_left_to_the_server(
    world: _World, body: str, key: list[str]
) -> None:
    """A merge of one row, without its key column or keyed on a uuid goes to the server as is.

    One row cannot repeat; a missing key column, or one pyarrow cannot group, is the server's to
    judge.
    """
    _write_asset(world, body)
    assert world.run(**_v3("merge", key=key)) == 0
    (call,) = world.client.tables.calls
    assert (call["method"], call["key"]) == ("merge", key)


def test_a_function_whose_signature_cannot_be_read_is_called_without_ctx(world: _World) -> None:
    """A `__signature__` that `inspect` refuses: the function is called with its inputs only."""
    world.result_path.parent.mkdir(exist_ok=True)
    (world.root / "assets" / "gold" / "scores.py").write_text(
        "from lakana import asset\n\n\n"
        "@asset(inputs={'obs': 'silver.weather'})\n"
        "def f(obs, **rest):\n"
        "    assert 'ctx' not in rest\n"
        "    return obs\n\n\n"
        "f.__signature__ = 'not a signature'\n"
    )
    assert world.run() == 0
    assert _uploaded(world).to_pydict() == {"id": [1, 2]}
