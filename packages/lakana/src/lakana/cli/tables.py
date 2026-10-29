"""`lakana table *`: manage tables and load data into them."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

import lakana
from lakana._models import (
    CheckHealth,
    Column,
    DroppedTable,
    Lineage,
    ModelReference,
    Table,
    TableHealth,
)
from lakana.cli import _files, _output, _retention
from lakana.cli._break_glass import BREAK_GLASS, REASON, break_glass_reason
from lakana.cli._state import CliError, CliState, confirm, resolve_environment, state_of
from lakana.cli.artifacts import human_size
from lakana.cli.checks import check_shown

if TYPE_CHECKING:
    import pyarrow as pa

table_app = typer.Typer(help="Manage tables and load data.", no_args_is_help=True)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_FORMAT = Annotated[
    str | None,
    typer.Option("--format", help="parquet, csv or arrow (default: from the extension)."),
]
_SOURCE = Annotated[
    list[str] | None,
    typer.Option("--source", help="A table the data came from (repeatable); recorded as lineage."),
]


def parse_column_spec(spec: str) -> dict[str, object]:
    """Parse `name:type[?]` into the API's JSON column form.

    Parameters
    ----------
    spec
        E.g. `"id:long"` or `"amount:decimal(10,2)?"` (`?` = nullable).

    Returns
    -------
    dict
        `{"name", "type", "nullable"}`.

    Raises
    ------
    CliError
        If the name or type is missing (exit 2).
    """
    name, sep, type_part = spec.partition(":")
    nullable = type_part.endswith("?")
    type_name = type_part[:-1] if nullable else type_part
    if not sep or not name or not type_name:
        raise CliError(f"--column expects name:type or name:type?, got {spec!r}")
    return {"name": name, "type": type_name, "nullable": nullable}


def _load_schema_file(path: Path) -> list[dict[str, object]]:
    """Read a JSON column list from a file.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    list of dict
        The column list.

    Raises
    ------
    CliError
        If the file is missing, not JSON, or not a list (exit 8).
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"Could not read schema file {path}: {exc}", 8, kind="FileError") from exc
    if not isinstance(data, list):
        raise CliError(f"{path} must contain a JSON list of columns", 8, kind="FileError")
    return data


@table_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    schema: Annotated[
        Path | None, typer.Option("--schema", help="JSON file with the column list.")
    ] = None,
    column: Annotated[
        list[str] | None,
        typer.Option(
            "--column",
            help=(
                "name:type, ? suffix = nullable (repeatable). Quote a ? spec, "
                "e.g. 'region:string?', so the shell doesn't glob it."
            ),
        ),
    ] = None,
    expire_after: _retention.EXPIRE_AFTER = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Create managed table NAME (namespace.table).

    With --expire-after N the table expires N days after its last write or read: the worker's
    clean-up then drops it, and `lakana table undrop` restores it during the undo period.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    schema
        A JSON column-list file.
    column
        Column specs.
    expire_after
        `--expire-after`: the days after its last activity the table expires.
    break_glass
        `--break-glass`.
    reason
        `--reason`.

    Raises
    ------
    CliError
        Unless exactly one of `--schema`/`--column` is given, or with `--expire-after` and
        `--break-glass` together (exit 2).
    """
    state = state_of(ctx)
    if (schema is None) == (not column):
        raise CliError("Pass exactly one of --schema FILE or --column name:type")
    if expire_after is not None and break_glass:
        raise CliError(_NO_EXPIRY_WITH_BREAK_GLASS)
    columns = (
        _load_schema_file(schema)
        if schema is not None
        else [parse_column_spec(c) for c in column or []]
    )
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    client = state.client()
    table = client.tables.create(environment, name, schema=columns, break_glass_reason=glass)
    created = f"Created table {_output.inline(environment)}/{_output.inline(name)}"
    if expire_after is not None:
        created += _retention.set_expiry(client, environment, name, expire_after, created=created)
    _output.done(state, created, table)


_NO_EXPIRY_WITH_BREAK_GLASS = (
    "--expire-after cannot go with --break-glass: tables in a protected environment never "
    "expire. Leave out --expire-after."
)
"""Why `--expire-after` is refused together with `--break-glass`."""


@table_app.command("create-external")
def create_external(
    ctx: typer.Context,
    name: str,
    location: Annotated[
        str, typer.Option("--location", help="Pattern under raw/, e.g. raw/orders/*.csv")
    ],
    file_format: Annotated[str, typer.Option("--format", help="csv, json or parquet")],
    env: _ENV = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Register external table NAME over files under raw/.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    location
        The location pattern.
    file_format
        The files' format.
    env
        The environment.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    table = state.client().tables.create_external(
        environment, name, location, file_format, break_glass_reason=glass
    )
    _output.done(
        state, f"Created external table {_output.inline(environment)}/{_output.inline(name)}", table
    )


def _state_cell(table: Table) -> str:
    """Say whether a table is published, for the `state` column.

    Parameters
    ----------
    table
        A table, view, external table or artifact as the server described it.

    Returns
    -------
    str
        ``published``, ``not published`` or ``kept`` for a managed table; empty for a view, an
        external table and an artifact, which have no first build to fail.
    """
    if table.kind != "TABLE":
        return ""
    return {"not_published": "not published", "kept": "kept"}.get(table.state, "published")


_GET_COLUMNS = ["name", "namespace", "kind", "state", "uuid"]
"""The rows `table get` shows for every kind; `state` is empty where a kind has none."""
_DERIVED = {"state": _state_cell}


def _restorable_cell(dropped: DroppedTable) -> str:
    """Say whether a dropped table can still be restored, for the `restorable` column.

    Parameters
    ----------
    dropped
        A dropped table as the server described it.

    Returns
    -------
    str
        ``yes``, or ``no`` once its undo period has ended or its files are being deleted.
    """
    return "yes" if dropped.restorable else "no"


@table_app.command("list")
def list_tables(
    ctx: typer.Context,
    namespace: str,
    dropped: Annotated[
        bool,
        typer.Option(
            "--dropped",
            help="List the dropped tables that can still be restored, and until when.",
        ),
    ] = False,
    model_kind: Annotated[
        str | None,
        typer.Option(
            "--model-kind",
            help="Only tables whose asset header declares this kind: fact, dimension, wide or "
            "staging.",
        ),
    ] = None,
    env: _ENV = None,
) -> None:
    """List the tables in NAMESPACE, or with --dropped the tables dropped from it.

    A dropped table keeps its files until `purge_after` (the server's undo period, 7 days by
    default); until then `lakana table undrop` restores it. A table that its retention lets
    expire within the server's warning window (7 days by default) shows when, in `expires`.
    With --model-kind, only the tables whose asset header says `kind: <that kind>`.

    Parameters
    ----------
    ctx
        The Typer context.
    namespace
        The dotted namespace.
    dropped
        List the dropped tables instead.
    model_kind
        Only tables whose header declares this kind.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    if dropped:
        _output.emit(
            state,
            state.client().tables.list_dropped(environment, namespace),
            columns=["name", "namespace", "dropped_at", "purge_after", "restorable", "uuid"],
            droppable=["uuid", "namespace"],
            derived={"restorable": _restorable_cell},
        )
        return
    tables = state.client().tables.list(environment, namespace, model_kind=model_kind)
    columns = ["name", "namespace", "kind", "state", "uuid"]
    if any(table.expires_soon for table in tables):
        columns.insert(4, "expires")
    _output.emit(
        state,
        tables,
        columns=columns,
        droppable=["kind", "uuid"],
        derived={"state": _state_cell, "expires": _expires_cell},
    )


def _expires_cell(table: Table) -> str:
    """Say when a table expires, for the `expires` column, within the server's warning window.

    Parameters
    ----------
    table
        A table as the server described it.

    Returns
    -------
    str
        ``in 3 days (date)`` and the like for a table that expires soon; empty otherwise.
    """
    return _retention.expires_cell(table.expires_at) if table.expires_soon else ""


def _emit_description(state: CliState, table: Table) -> None:
    """Print a table's description and where it came from, when it has one.

    Parameters
    ----------
    state
        The CLI state.
    table
        The table.
    """
    if table.description:
        source = "edited" if table.description_source == "edited" else "from the project"
        _output.emit(state, f"Description ({source}): {table.description}")


def _emit_columns(state: CliState, table: Table) -> None:
    """Print a table's columns, with their descriptions when any has one.

    Parameters
    ----------
    state
        The CLI state.
    table
        The table.
    """
    if any(c.description for c in table.columns):
        _output.emit(state, table.columns, columns=_DESCRIBE_COLUMNS, flexible=["description"])
    else:
        _output.emit(state, table.columns)


@table_app.command("get")
def get(ctx: typer.Context, name: str, env: _ENV = None) -> None:
    """Show table or view NAME, its columns, and a view's SQL and references.

    A table whose first build did not finish, or failed a check, is shown as `not published`;
    one that keeps the data that failed a check (`on_failure: keep`), as `kept`. A table that
    its retention lets expire within the server's warning window says when.
    An artifact is shown with its name and a pointer to `lakana artifact versions list`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    """
    state = state_of(ctx)
    table = state.client().tables.get(resolve_environment(state, env), name)
    if state.output == "json":
        _output.emit(state, table)
        return
    if table.kind == "VIEW":
        _output.emit(state, table, columns=_GET_COLUMNS, derived=_DERIVED)
        if table.hidden_references:
            _output.emit(
                state,
                f"SQL hidden: {table.hidden_references} referenced table(s) you can't read",
            )
        else:
            _output.emit(state, table.sql or "")
        _output.emit(state, table.references)
        _emit_description(state, table)
        _emit_columns(state, table)
        return
    if table.kind == "ARTIFACT":
        _output.emit(state, table, columns=_GET_COLUMNS, derived=_DERIVED)
        _output.emit(
            state,
            f"{_output.inline(name)} is an artifact. See its versions with: "
            f"lakana artifact versions list {shlex.quote(_output.inline(name))}",
        )
        return
    _output.emit(state, table, columns=[*_GET_COLUMNS, "metadata_location"], derived=_DERIVED)
    if table.state_message:
        _output.emit(state, table.state_message)
    if table.expires_soon and table.expires_at is not None:
        environment = resolve_environment(state, env)
        _output.emit(
            state,
            f"This table {_retention.expiry_text(table.expires_at)}. Keep it with: lakana table "
            f"retention {shlex.quote(_output.inline(name))} --env "
            f"{shlex.quote(_output.inline(environment))} --never",
        )
    _emit_description(state, table)
    _emit_columns(state, table)
    if table.model is not None:
        _emit_model(state, table)


def _hidden_outgoing(count: int) -> str:
    """Say how many references point at tables the reader may not read.

    Parameters
    ----------
    count
        How many; more than 0.

    Returns
    -------
    str
        The line.
    """
    if count == 1:
        return "1 more reference to a table you cannot read"
    return f"{count} more references to tables you cannot read"


def _reference_line(ref: ModelReference) -> str:
    """Write one reference as ``column → target.column (exists)``.

    Parameters
    ----------
    ref
        The reference.

    Returns
    -------
    str
        The line, indented, with control characters escaped.
    """
    exists = "exists" if ref.target_exists else "missing"
    return _output.inline(f"  {ref.column} → {ref.target}.{ref.target_column} ({exists})")


def _emit_model(state: CliState, table: Table) -> None:
    """Print the Model block: kind, grain, key, references and the generated checks.

    Parameters
    ----------
    state
        The CLI state.
    table
        The table, with `model` set.
    """
    model = table.model
    assert model is not None  # nosec B101 -- the caller checked
    _output.emit(state, _output.inline(f"Model: {model.kind or '(no kind)'}"))
    if model.grain:
        _output.emit(state, _output.inline(f"Grain: {model.grain}", 500))
    key = "none — grain not checked" if model.key_none else ", ".join(model.key)
    _output.emit(state, _output.inline(f"Key: {key or '(none declared)'}"))
    for ref in model.references:
        _output.emit(state, _reference_line(ref))
    if model.hidden_references:
        _output.emit(state, f"  {_hidden_outgoing(model.hidden_references)}")
    if model.checks:
        _output.emit(
            state,
            list(model.checks),
            columns=["check", "state", "failing_rows", "finished_at"],
            derived={"check": check_shown, "state": _check_state},
        )


_DESCRIBE_COLUMNS = ["name", "type", "nullable", "description"]
"""The columns of a table's schema listing in `table get`."""


def _utc(moment: datetime) -> str:
    """Write a time as people read it.

    Parameters
    ----------
    moment
        A time with a zone.

    Returns
    -------
    str
        ``2026-10-11 12:00 UTC``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _plural(count: int, noun: str) -> str:
    """Write a count and its noun.

    Parameters
    ----------
    count
        The count.
    noun
        The noun in the singular.

    Returns
    -------
    str
        ``1 tag``, ``3 tags``.
    """
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _maintenance_line(health: TableHealth, environment: str, name: str) -> str:
    """Say what the last maintenance did, or how to run it.

    Parameters
    ----------
    health
        The table's health.
    environment
        The environment.
    name
        The qualified table name.

    Returns
    -------
    str
        One line.
    """
    last = health.last_maintenance
    if last is None:
        return (
            "Last maintenance: none yet. Run it with: "
            f"lakana table maintain {shlex.quote(_output.inline(name))} "
            f"--env {shlex.quote(_output.inline(environment))}"
        )
    line = (
        f"Last maintenance: {_utc(last.finished_at)}, {last.status}. "
        f"Expired {_plural(last.snapshots_expired, 'snapshot')}, "
        f"deleted {_plural(last.files_deleted, 'file')} ({human_size(last.bytes_deleted)})"
    )
    if last.tags_removed:
        line += f", removed {_plural(last.tags_removed, 'tag')} of failed checks"
    if last.status == "stopped":
        line += ". It stopped at its time limit; run it again to continue where it stopped"
    return line


def _check_state(check: CheckHealth) -> str:
    """Name a check's state as the check history does.

    Parameters
    ----------
    check
        The check's newest result.

    Returns
    -------
    str
        The outcome, with ``(kept)`` for a failure whose data was kept and ``(not published)``
        for one whose table was left not published.
    """
    if check.kept:
        return f"{check.outcome} (kept)"
    if check.not_published:
        return f"{check.outcome} (not published)"
    return check.outcome


@table_app.command("health")
def health(
    ctx: typer.Context,
    name: str,
    days: Annotated[
        int | None,
        typer.Option(
            "--days",
            min=1,
            help="Readers over this many days (up to the server's kept period, 90 by default).",
        ),
    ] = None,
    env: _ENV = None,
) -> None:
    """Show table NAME's last maintenance, the newest result of each check, and who reads it.

    Readers are counted from the queries that succeeded, per person and day. You see them only
    with the ADMIN role. A Python task's reads of its inputs count too. Use --output json for every
    field.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    days
        The window of the readers in days.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    found = state.client().tables.health(environment, name, days=days)
    if state.output == "json":
        _output.emit(state, found)
        return
    _output.emit(state, _maintenance_line(found, environment, name))
    if found.checks:
        _output.emit(
            state,
            found.checks,
            columns=["check", "state", "failing_rows", "finished_at"],
            derived={"check": check_shown, "state": _check_state},
        )
    else:
        _output.emit(state, "Checks: none recorded.")
    if found.readers is None:
        _output.emit(state, "Readers: not shown. Seeing who reads a table needs the ADMIN role.")
    elif found.readers:
        _output.emit(state, f"Readers, last {found.days} days:")
        _output.emit(state, found.readers)
    else:
        _output.emit(state, f"Readers, last {found.days} days: none.")


@table_app.command("describe")
def describe(
    ctx: typer.Context,
    name: str,
    column: Annotated[
        str | None,
        typer.Option("--column", help="Describe this column instead of the table."),
    ] = None,
    text: Annotated[
        str | None,
        typer.Option("--text", help="The description, up to 4,000 characters."),
    ] = None,
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Remove your text, so the project's shows again."),
    ] = False,
    env: _ENV = None,
) -> None:
    """Describe table NAME or one of its columns, or reset the description.

    The text you set is shown instead of the `description:` in the asset's header, and a deploy
    never replaces it. `--reset` goes back to the header's text. You need to own the table, or
    hold the right to drop it.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    column
        A column to describe instead of the table.
    text
        The new description.
    reset
        Remove the edit.
    env
        The environment.

    Raises
    ------
    CliError
        If neither or both of `--text` and `--reset` are given.
    """
    if (text is None) == (not reset):
        raise CliError(
            "Give exactly one of --text TEXT (set the description) and --reset "
            "(go back to the project's text)."
        )
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    table = state.client().tables.describe(
        environment, name, None if reset else text, column=column
    )
    target = f"{_output.inline(name)}" + (f" column {_output.inline(column)}" if column else "")
    if not reset:
        _output.done(state, f"Described {target} in {_output.inline(environment)}", table)
        return
    shown = (
        table.description
        if column is None
        else next((c.description for c in table.columns if c.name == column), None)
    )
    after = "now shows the project's text" if shown else "now has no description"
    _output.done(
        state,
        f"Reset the description of {target} in {_output.inline(environment)}; it {after}",
        table,
    )


@table_app.command("delete")
def delete(
    ctx: typer.Context,
    name: str,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Delete its files at the next clean-up instead; it cannot be restored.",
        ),
    ] = False,
    env: _ENV = None,
    yes: _YES = False,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Drop table NAME. Its files are kept for the undo period, then deleted.

    Within the server's undo period (`LAKANA_TABLES__UNDO_DAYS`, 7 days by default), `lakana
    table undrop NAME` restores the table with its data and its grants, row filters and column
    masks. `--purge` skips the period: the worker deletes the files at its next clean-up. An
    external table's files are never deleted.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    purge
        Make the files due at once.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    target = f"{environment}/{name}"
    if purge:
        question = (
            f"Delete table {target} and its files at the next clean-up? This cannot be undone."
        )
    else:
        question = f"Delete table {target}? It can be restored during the undo period."
    confirm(state, question, yes)
    state.client().tables.delete(environment, name, purge=purge, break_glass_reason=glass)
    deleted = f"Deleted table {_output.inline(environment)}/{_output.inline(name)}"
    if purge:
        _output.done(
            state,
            f"{deleted}. Its files are deleted at the next clean-up; it cannot be restored.",
        )
        return
    namespace = name.rsplit(".", 1)[0]
    _output.done(
        state,
        f"{deleted}. Restore it during the undo period with "
        f"`lakana table undrop {_output.inline(name)} --env {_output.inline(environment)}`; "
        f"`lakana table list {_output.inline(namespace)} --env {_output.inline(environment)} "
        "--dropped` shows until when.",
    )


@table_app.command("undrop")
def undrop(
    ctx: typer.Context,
    name: str,
    new_name: Annotated[
        str | None,
        typer.Option(
            "--as", help="Restore it under this name, when a table of its own name exists now."
        ),
    ] = None,
    env: _ENV = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Restore the dropped table NAME, with its data, grants, row filters and column masks.

    Works until the end of the undo period that `lakana table list NAMESPACE --dropped` shows.
    Needs the rights to drop and to create tables in the namespace. When NAME was dropped more
    than once, the newest comes back; restore an older one with `--as`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified name the table had.
    new_name
        `--as`: the name to restore it under, in the same namespace.
    env
        The environment.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    table = state.client().tables.undrop(
        environment, name, new_name=new_name, break_glass_reason=glass
    )
    message = f"Restored table {_output.inline(environment)}/{_output.inline(name)}"
    if new_name is not None:
        message += f" as {_output.inline(f'{table.namespace}.{table.name}')}"
    _output.done(state, message, table)


def _table_columns(client: lakana.Client, environment: str, name: str) -> list[Column]:
    """Fetch a table's columns, to conform a file to them.

    Parameters
    ----------
    client
        The SDK client.
    environment
        The environment.
    name
        The qualified table name.

    Returns
    -------
    list of Column
        The table's columns.

    Raises
    ------
    CliError
        If the server reports no columns (an older server; exit 11).
    """
    table = client.tables.get(environment, name)
    if not table.columns:
        # A table always has at least one column; an empty list means the server
        # is too old to report schemas, so there is nothing to conform to.
        raise CliError(
            f"The server didn't report {environment}/{name}'s columns; upgrade the Lakana "
            "server to a version that reports a table's columns to use this command.",
            11,
            kind="IncompatibleServer",
        )
    return table.columns


@dataclass(frozen=True)
class _Upload:
    """What a command that sends a file to a table has read before it sends.

    Parameters
    ----------
    state
        The CLI state.
    environment
        The environment.
    glass
        The break-glass reason, if any.
    client
        The SDK client.
    data
        The file's rows, fitted to the table's columns unless the command said not to.
    """

    state: CliState
    environment: str
    glass: str | None
    client: lakana.Client
    data: pa.Table


def _read_upload(
    ctx: typer.Context,
    name: str,
    file: Path,
    *,
    env: str | None,
    file_format: str | None,
    break_glass: bool,
    reason: str | None,
    conform: bool = True,
) -> _Upload:
    """Resolve the environment and read FILE for `table append`, `replace` or `merge`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    file
        The data file.
    env
        `--env`.
    file_format
        `--format`.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    conform
        Fetch the table's columns and fit the file to them; off sends the file as it is.

    Returns
    -------
    _Upload
        What to send, and where.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    fmt = _files.infer_format(file, file_format)
    client = state.client()
    if conform:
        columns = _table_columns(client, environment, name)
        data = _files.conform(_files.read_table(file, fmt, columns), columns)
    else:
        data = _files.read_table(file, fmt)
    return _Upload(state=state, environment=environment, glass=glass, client=client, data=data)


@table_app.command("append")
def append(
    ctx: typer.Context,
    name: str,
    file: Path,
    env: _ENV = None,
    file_format: _FORMAT = None,
    source: _SOURCE = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Append FILE (Parquet, CSV or Arrow) to table NAME.

    The table's schema is fetched first. A CSV is parsed straight into the
    table's column types; Parquet and Arrow are cast to them. Nullability is
    checked too, and a column that doesn't fit fails here, before anything
    is sent. A CSV must be UTF-8 (a byte order mark is dropped); one in
    another encoding is refused (exit 8): convert it to UTF-8 and run the
    command again.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    file
        The data file.
    env
        The environment.
    file_format
        The file's format, if the extension doesn't say.
    source
        Tables the data came from, recorded as lineage.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    upload = _read_upload(
        ctx,
        name,
        file,
        env=env,
        file_format=file_format,
        break_glass=break_glass,
        reason=reason,
    )
    environment = upload.environment
    result = upload.client.tables.append(
        environment, name, upload.data, sources=source or None, break_glass_reason=upload.glass
    )
    _output.done(
        upload.state,
        f"Appended {result.rows} rows to {_output.inline(environment)}/{_output.inline(name)}",
        result,
    )


@table_app.command("replace")
def replace(
    ctx: typer.Context,
    name: str,
    file: Path,
    env: _ENV = None,
    file_format: _FORMAT = None,
    evolve_schema: Annotated[
        bool, typer.Option("--evolve-schema", help="Let the table adopt the file's schema.")
    ] = False,
    source: _SOURCE = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Replace every row of table NAME with FILE (Parquet, CSV or Arrow).

    Without --evolve-schema the file is conformed to the table's columns
    first, exactly as `table append` does. With it, the file is sent as it
    is and the table takes its schema. A CSV must be UTF-8 either way, as
    for `table append`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    file
        The data file.
    env
        The environment.
    file_format
        The file's format, if the extension doesn't say.
    evolve_schema
        Let the table adopt the file's schema.
    source
        Tables the data came from, recorded as lineage.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    upload = _read_upload(
        ctx,
        name,
        file,
        env=env,
        file_format=file_format,
        break_glass=break_glass,
        reason=reason,
        conform=not evolve_schema,
    )
    environment = upload.environment
    result = upload.client.tables.replace(
        environment,
        name,
        upload.data,
        evolve_schema=evolve_schema,
        sources=source or None,
        break_glass_reason=upload.glass,
    )
    _output.done(
        upload.state,
        f"Replaced {_output.inline(environment)}/{_output.inline(name)} with {result.rows} rows",
        result,
    )


@table_app.command("merge")
def merge(
    ctx: typer.Context,
    name: str,
    file: Path,
    key: Annotated[
        list[str],
        typer.Option(
            "--key",
            help="A column that identifies a row (repeat it for a key of several columns).",
        ),
    ],
    env: _ENV = None,
    file_format: _FORMAT = None,
    source: _SOURCE = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Merge FILE (Parquet, CSV or Arrow) into table NAME on --key.

    A row whose key the table has replaces that row; any other row is added; nothing is
    deleted, all in one snapshot. The file is conformed to the table's columns first, exactly
    as `table append` does. The file must not repeat a key and no key may be empty. A CSV must
    be UTF-8 (a byte order mark is dropped); one in another encoding is refused (exit 8).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    file
        The data file.
    key
        `--key`: the columns that identify a row.
    env
        The environment.
    file_format
        The file's format, if the extension doesn't say.
    source
        Tables the data came from, recorded as lineage.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    upload = _read_upload(
        ctx,
        name,
        file,
        env=env,
        file_format=file_format,
        break_glass=break_glass,
        reason=reason,
    )
    environment = upload.environment
    result = upload.client.tables.merge(
        environment,
        name,
        upload.data,
        key=key,
        sources=source or None,
        break_glass_reason=upload.glass,
    )
    _output.done(
        upload.state,
        f"Merged into {_output.inline(environment)}/{_output.inline(name)}: "
        f"{result.rows_inserted} rows inserted, {result.rows_updated} updated",
        result,
    )


_FAILED_CHECK_TAG_DAYS = 7
"""Days after which maintenance removes the tag of a failed check (the server's rule)."""


def _maintained(environment: str, name: str, report: dict[str, object]) -> str:
    """Describe a finished maintenance run in one line.

    Parameters
    ----------
    environment
        The environment.
    name
        The table.
    report
        The run's `maintenance["report"]`.

    Returns
    -------
    str
        What was (or, for a dry run, would be) expired and deleted, the tags of
        failed checks removed (when there were any), then the report's note, if any.
    """
    expired_ids = report.get("snapshots_expired")
    expired = len(expired_ids) if isinstance(expired_ids, list) else 0
    files, size = report.get("files_deleted", 0), report.get("bytes_deleted", 0)
    if report.get("dry_run"):
        line = (
            f"Dry run for {_output.inline(environment)}/{_output.inline(name)}: "
            f"would expire {expired} snapshots, would delete {files} files ({size} bytes)"
        )
    else:
        line = (
            f"Maintained {_output.inline(environment)}/{_output.inline(name)}: "
            f"expired {expired} snapshots, deleted {files} files ({size} bytes)"
        )
    removed = report.get("tags_removed")
    tags = len(removed) if isinstance(removed, list) else 0
    if tags:
        verb = "would remove" if report.get("dry_run") else "removed"
        what = "1 tag of a failed check" if tags == 1 else f"{tags} tags of failed checks"
        line += f", {verb} {what} older than {_FAILED_CHECK_TAG_DAYS} days"
    note = report.get("note")
    return f"{line}. {_output.inline(str(note), limit=1000)}" if note else line


@table_app.command("maintain")
def maintain(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    keep_days: Annotated[
        int, typer.Option("--keep-days", min=0, help="Keep snapshots younger than this.")
    ] = 7,
    keep_last: Annotated[
        int, typer.Option("--keep-last", min=1, help="Always keep this many recent snapshots.")
    ] = 5,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would happen; change nothing.")
    ] = False,
    older_than_hours: Annotated[
        int,
        typer.Option(
            "--older-than-hours",
            min=1,
            max=87600,
            help="Orphaned files younger than this many hours are never deleted (1 to 87600).",
        ),
    ] = 72,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds to wait before giving up.")
    ] = None,
) -> None:
    """Expire table NAME's old snapshots, delete its orphaned files, and remove failed-check tags.

    A snapshot kept under the tag of a failed check is removed with that tag once it is older
    than 7 days. A snapshot that a run still in progress needs to set its table back is kept.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    keep_days
        Snapshots younger than this many days are kept.
    keep_last
        The most recent this many snapshots are kept.
    dry_run
        Report only.
    older_than_hours
        Orphans younger than this many hours are never deleted (1 to 87600).
    timeout
        Seconds to wait for the run.

    Raises
    ------
    CliError
        With exit code 1 if the report's `conflict` is set: the table
        changed during maintenance and nothing was applied; the caller
        should retry. The note's wording is never compared.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    run = state.client().tables.maintain(
        environment,
        name,
        keep_days=keep_days,
        keep_last=keep_last,
        dry_run=dry_run,
        older_than_hours=older_than_hours,
    )
    status = run.wait(timeout=timeout)
    report = (status.maintenance or {}).get("report", {})
    if report.get("conflict") is True:
        raise CliError(
            f"Maintenance of {environment}/{name} not applied: "
            "table changed during maintenance; retry",
            1,
            kind="MaintenanceNotApplied",
        )
    _output.done(state, _maintained(environment, name, report), status)


def lineage_tree(lineage: Lineage) -> list[str]:
    """Render a lineage graph as tree lines, the root first.

    A node is expanded once, at its first occurrence; later occurrences
    (a self-edge, a diamond, a shared ancestor) print as ``(see above)``, so
    the output is linear in the edges however the graph converges. A connection the
    writing asset declared prints as ``connection <name> (<kind>)``, or
    ``(deleted)`` once it no longer exists.

    Parameters
    ----------
    lineage
        The graph.

    Returns
    -------
    list of str
        One line per node, plus a final line counting hidden nodes.
    """
    nodes = {node.id: node for node in lineage.nodes}
    children: dict[str, list[str]] = {}
    for edge in lineage.edges:
        parent, child = (
            (edge.target_id, edge.source_id)
            if lineage.direction == "upstream"
            else (edge.source_id, edge.target_id)
        )
        children.setdefault(parent, []).append(child)

    def label(node_id: str) -> str:
        node = nodes[node_id]
        if node.kind == "CONNECTION":
            detail = "deleted" if node.dropped else node.connection_kind or "connection"
            return f"connection {node.name} ({detail})"
        text = node.name
        if node.kind not in (None, "TABLE"):
            text += f" [{node.kind.lower()}]"
        if node.dropped:
            text += " (dropped)"
        return text

    lines = [label(lineage.root_id)]
    expanded = {lineage.root_id}

    def walk(node_id: str, prefix: str) -> None:
        kids = sorted(children.get(node_id, []), key=lambda kid: nodes[kid].name)
        for index, kid in enumerate(kids):
            last = index == len(kids) - 1
            text = label(kid)
            if kid in expanded:
                text += " (see above)"
            lines.append(f"{prefix}{'└── ' if last else '├── '}{text}")
            if kid not in expanded:
                expanded.add(kid)
                walk(kid, prefix + ("    " if last else "│   "))

    walk(lineage.root_id, "")
    if lineage.hidden_nodes == 1:
        lines.append("(1 more node hidden: you can't read it)")
    elif lineage.hidden_nodes:
        lines.append(f"({lineage.hidden_nodes} more nodes hidden: you can't read them)")
    if lineage.truncated:
        lines.append("… truncated at 500 nodes")
    return lines


@table_app.command("lineage")
def lineage(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    downstream: Annotated[
        bool, typer.Option("--downstream", help="Show what NAME feeds, not what feeds it.")
    ] = False,
    depth: Annotated[int, typer.Option("--depth", min=1, max=10, help="Hops to walk.")] = 3,
) -> None:
    """Show table NAME's lineage as a tree (or JSON with --output json).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    downstream
        Walk downstream instead of upstream.
    depth
        Hops to walk.
    """
    state = state_of(ctx)
    graph = state.client().tables.lineage(
        resolve_environment(state, env),
        name,
        direction="downstream" if downstream else "upstream",
        depth=depth,
    )
    if state.output == "json":
        _output.emit(state, graph)
        return
    typer.echo("\n".join(lineage_tree(graph)))


@table_app.command("references")
def references(ctx: typer.Context, name: str, env: _ENV = None) -> None:
    """Show what table NAME's asset header references, and which tables reference it.

    These are the references the model declares (`references:` in the asset header), not
    lineage: lineage is how data flowed, references are what the model says relates. A
    reference to or from a table you cannot read is counted, never named.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    env
        The environment.
    """
    state = state_of(ctx)
    found = state.client().tables.references(resolve_environment(state, env), name)
    if state.output == "json":
        _output.emit(state, found)
        return
    sections = (
        ("References", found.outgoing, ["column", "target", "target_column", "target_exists"]),
        ("Referenced by", found.incoming, ["table", "column", "target_column"]),
    )
    for title, listed, columns in sections:
        if listed:
            _output.emit(state, f"{title}:")
            _output.emit(state, list(listed), columns=columns)
        else:
            _output.emit(state, f"{title}: none.")
        if title == "References" and found.hidden_outgoing:
            _output.emit(state, _hidden_outgoing(found.hidden_outgoing))
    if found.hidden_incoming:
        tables = "a table" if found.hidden_incoming == 1 else "tables"
        _output.emit(state, f"{found.hidden_incoming} more from {tables} you cannot read")


@table_app.command("retention")
def retention(
    ctx: typer.Context,
    name: str,
    days: Annotated[
        int | None,
        typer.Option("--days", min=1, max=3650, help="Expire N days after the last write or read."),
    ] = None,
    on: Annotated[
        str | None, typer.Option("--on", help="Expire on this day (YYYY-MM-DD, UTC).")
    ] = None,
    never: Annotated[
        bool, typer.Option("--never", help="Never expire, whatever the environment says.")
    ] = False,
    env: _ENV = None,
) -> None:
    """Show when managed table NAME expires, or set it with --days, --on or --never.

    An expired table is dropped by the worker's clean-up and can be restored with `lakana
    table undrop` during the undo period. Its last activity is the later of its last write
    (or other change) and its last read through Lakana's queries. Tables in a protected
    environment and tables a deployed project builds never expire.

    Without an option this shows the table's retention. The table's owner may set any.
    Otherwise, making it expire sooner (or at all) needs the right to drop the table; making it
    expire later, or --never, needs the right to write it.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified table name.
    days
        `--days`.
    on
        `--on`.
    never
        `--never`.
    env
        The environment.

    Raises
    ------
    CliError
        With more than one of the options, or a malformed day (exit 2).
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    given = sum((days is not None, on is not None, never))
    if given > 1:
        raise CliError("Give one of --days, --on or --never, not several.")
    day = None if on is None else _retention.parse_day(on)
    tables = state.client().tables
    if given == 0:
        shown = tables.retention(environment, name)
        if state.output == "json":
            _output.emit(state, shown)
            return
        _output.emit(state, _retention.describe(environment, shown))
        return
    changed = tables.set_retention(environment, name, days=days, on=day, never=never)
    message = f"Set the retention of {_output.inline(environment)}/{_output.inline(name)}. "
    _output.done(state, message + _retention.describe(environment, changed), changed)


@table_app.command("expiring")
def expiring(
    ctx: typer.Context,
    within: Annotated[
        str | None,
        typer.Option(
            "--within",
            help="List what expires within this many days, such as 7d (default: the "
            "server's warning window, 7 days).",
        ),
    ] = None,
    env: _ENV = None,
) -> None:
    """List the managed tables that their retention lets expire soon, soonest first.

    Each row says when, the rule (and whether the table or its environment sets it) and who set
    it. Keep a table with `lakana table retention NAME --never`, or give it more days.

    Parameters
    ----------
    ctx
        The Typer context.
    within
        `--within`.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    days = None if within is None else _retention.parse_within(within)
    found = state.client().tables.expiring(environment, within_days=days)
    if not found and state.output != "json":
        window = "the warning window" if days is None else f"{days} days"
        typer.echo(f"No table in {_output.inline(environment)} expires within {window}.")
        return
    _output.emit(
        state,
        found,
        columns=["table", "expires", "rule", "set_by"],
        derived={
            "table": lambda entry: f"{entry.namespace}.{entry.name}",
            "expires": lambda entry: _retention.expires_cell(entry.expires_at),
            "rule": _retention.rule_text,
        },
        flexible=["rule"],
    )


def register(app: typer.Typer) -> None:
    """Add the `table` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(table_app, name="table")
