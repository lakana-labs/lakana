"""`lakana env *`: manage environments."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated
from urllib.parse import urlsplit

import typer

from lakana._models import (
    EnvironmentUsage,
    OrphanedLocation,
    OrphanedLocations,
    UsagePart,
    WorkerInfo,
)
from lakana.cli import _output
from lakana.cli._retention import parse_within
from lakana.cli._state import CliError, CliState, confirm, parse_key_values, state_of
from lakana.cli.artifacts import human_size
from lakana.errors import PermissionDenied

env_app = typer.Typer(help="Manage environments.", no_args_is_help=True)

_SCHEMES = ("file", "s3", "abfs")
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_OPTIONS = Annotated[
    list[str] | None,
    typer.Option(
        "--option",
        help=(
            "Storage option key=value (repeatable). Values land in shell history; "
            "prefer --secret-ref for credentials."
        ),
    ),
]
_STORAGE_ROOT_HELP = "e.g. file:///data or s3://bucket/lake"


@env_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    storage_root: Annotated[str, typer.Option("--storage-root", help=_STORAGE_ROOT_HELP)],
    option: _OPTIONS = None,
) -> None:
    """Create environment NAME (with bronze/silver/gold namespaces).

    The name must not be `instance` (in any case), contain a `.`, have blanks at either end or
    be longer than 255 characters: the access file reads `instance` as the whole instance and
    `prod.eu` as the namespace `eu` of `prod`. Write `prod-eu` instead.

    Write the storage root the one way Lakana compares locations: a lower-case scheme
    (`s3`, not `s3a`), one slash between path segments and none doubled after `file://`
    (`file:///data`, not `file:////data`), for `file` an absolute path and no host
    (not `file://data` or `file://localhost/data`), no `.` or `..` segments, no percent escapes
    and no `*`, `?`, `[` or `]`. One trailing slash is fine. Another spelling is refused, and the
    message names the one to use. The root of the disk (`file:///`) cannot hold an environment:
    use a directory, such as `file:///data/lakana`.

    Environments may share a root, and one may lie elsewhere in another's (prod at
    `s3://bucket`, dev at `s3://bucket/dev`). A root at or inside another environment's
    `managed/`, `raw/`, `artifacts/` or `_artifacts/`, a deleted environment's too, is refused,
    and so is one whose own such directory would hold another environment's root: the message
    names that environment and a root to use instead.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment name.
    storage_root
        Its storage root URI.
    option
        Storage options as key=value.

    Raises
    ------
    CliError
        If the root has no supported scheme (exit 2).
    """
    state = state_of(ctx)
    scheme = urlsplit(storage_root).scheme
    if scheme not in _SCHEMES:
        raise CliError(
            f"--storage-root must start with one of {', '.join(s + '://' for s in _SCHEMES)}; "
            f"got {storage_root!r}"
        )
    environment = state.client().environments.create(
        name,
        storage={
            "scheme": scheme,
            "root": storage_root,
            "options": parse_key_values(option, "--option", sensitive=True),
        },
    )
    _output.done(
        state,
        f"Created environment {_output.inline(name)} at {_output.inline(storage_root)}",
        environment,
    )


@env_app.command("list")
def list_environments(
    ctx: typer.Context,
    deleted: Annotated[
        bool,
        typer.Option(
            "--deleted",
            help="List the deleted environments that can still be restored, and until when.",
        ),
    ] = False,
) -> None:
    """List environments you can see, or with --deleted the deleted ones.

    A deleted environment keeps its tables' files until `purge_after` (the server's undo
    period, 7 days by default); until then `lakana env restore NAME` brings it back.

    Parameters
    ----------
    ctx
        The Typer context.
    deleted
        List the deleted environments instead.
    """
    state = state_of(ctx)
    if deleted:
        _output.emit(
            state,
            state.client().environments.list(deleted=True),
            columns=["name", "deleted_at", "purge_after", "storage_root"],
            droppable=["storage_root"],
        )
        return
    _output.emit(
        state,
        state.client().environments.list(),
        columns=["name", "storage_root", "is_protected"],
        droppable=["storage_root"],
    )


@env_app.command("get")
def get(ctx: typer.Context, name: str) -> None:
    """Show environment NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().environments.get(name))


@env_app.command("delete")
def delete(
    ctx: typer.Context,
    name: str,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help=(
                "Delete the tables' files at the worker's next clean-up instead of after the "
                "undo period; the environment cannot be restored then. Also for an environment "
                "already deleted."
            ),
        ),
    ] = False,
    yes: _YES = False,
) -> None:
    """Delete environment NAME. Its tables' files are kept for the undo period, then deleted.

    Within the server's undo period (`LAKANA_TABLES__UNDO_DAYS`, 7 days by default), `lakana
    env restore NAME` brings the environment back with its tables, their data and their grants.
    Until then the name stays taken, and `lakana env list --deleted` shows until when. Views
    and external tables are removed; an external table's files are never deleted. `--purge`
    skips the period.

    Refused (409) while any repository is deployed to it: run
    `lakana undeploy REPO --env NAME` for each first. Refused while protected.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    purge
        Make the files due at once.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    if purge:
        question = (
            f"Delete environment {name!r} and its tables' files at the next clean-up? This "
            "cannot be undone."
        )
    else:
        question = (
            f"Delete environment {name!r}? It can be restored with its tables during the undo "
            "period."
        )
    confirm(state, question, yes)
    deleted = state.client().environments.delete(name, purge=purge)
    shown = _output.inline(name)
    if deleted is None or deleted.purge_after is None:
        _output.done(state, f"Deleted environment {shown}", deleted)
    elif purge:
        _output.done(
            state,
            f"Deleted environment {shown}. Its tables' files are deleted at the next clean-up; "
            "it cannot be restored.",
            deleted,
        )
    else:
        until = deleted.purge_after.strftime("%Y-%m-%d %H:%M UTC")
        _output.done(
            state,
            f"Deleted environment {shown}. Restore it with its tables until {until} with "
            f"`lakana env restore {shown}`.",
            deleted,
        )


@env_app.command("restore")
def restore(ctx: typer.Context, name: str) -> None:
    """Restore the deleted environment NAME, with the tables, data and grants it had.

    Works until the end of the undo period that `lakana env list --deleted` shows, and not
    after `lakana env delete NAME --purge`. A table dropped before the environment was deleted
    stays dropped; restore it afterwards with `lakana table undrop`. Needs MANAGE_ENVIRONMENT
    (deleting the environment revoked the grants given on it, so usually an administrator's).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    environment = state.client().environments.restore(name)
    _output.done(state, f"Restored environment {_output.inline(name)}", environment)


@env_app.command("set-storage")
def set_storage(
    ctx: typer.Context,
    name: str,
    option: _OPTIONS = None,
    secret_ref: Annotated[
        str | None, typer.Option("--secret-ref", help="Secret holding credentials.")
    ] = None,
) -> None:
    """Change environment NAME's storage options or credentials secret.

    While the environment has a storage secret, changing the options also needs the right to
    manage secrets in it (the ADMIN role), because an option such as `s3.endpoint` decides
    where the secret is sent. `--option` replaces all the options, so pass every one the
    environment should have. Attaching a secret approves the options it has then.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    option
        New storage options as key=value.
    secret_ref
        The name of a secret holding storage credentials.

    Raises
    ------
    CliError
        If nothing would change (exit 2).
    """
    state = state_of(ctx)
    if not option and secret_ref is None:
        raise CliError("Nothing to change: pass --option and/or --secret-ref")
    environment = state.client().environments.update_storage(
        name,
        options=parse_key_values(option, "--option", sensitive=True) if option else None,
        secret_ref=secret_ref,
    )
    _output.done(state, f"Updated storage for environment {_output.inline(name)}", environment)


@env_app.command("protect")
def protect(ctx: typer.Context, name: str) -> None:
    """Protect environment NAME: lock writes down to deployed runs.

    A protected environment refuses every write outside runs of the deployed
    code (SQL, SDK, CLI and Iceberg REST clients alike) whatever the caller's
    grants; choose the identity a deployed job runs as with `lakana deployments set-run-as`. A
    holder of MANAGE_ENVIRONMENT can make one audited write with `--break-glass
    --reason`. It also refuses deletion and storage changes, and runs Python
    assets only on workers that run tasks in containers (LAKANA_RUN__RUNNER=docker).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    """
    state = state_of(ctx)
    environment = state.client().environments.set_protection(name, True)
    _output.done(state, f"Protected environment {_output.inline(name)}", environment)


@env_app.command("unprotect")
def unprotect(ctx: typer.Context, name: str, yes: _YES = False) -> None:
    """Remove protection from environment NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Remove protection from environment {name!r}?", yes)
    environment = state.client().environments.set_protection(name, False)
    _output.done(state, f"Unprotected environment {_output.inline(name)}", environment)


@env_app.command("retention")
def retention(
    ctx: typer.Context,
    name: str,
    days: Annotated[
        int | None,
        typer.Option(
            "--days",
            min=1,
            max=3650,
            help="Tables that set no retention expire N days after their last write or read.",
        ),
    ] = None,
    off: Annotated[
        bool, typer.Option("--off", help="Turn the environment's retention off.")
    ] = False,
    yes: _YES = False,
) -> None:
    """Show environment NAME's retention, or set it with --days or turn it off with --off.

    The retention applies to every managed table in the environment that sets none of its own
    (`lakana table retention`). Such a table expires N days after its last activity, and never
    sooner than N days after this command, so turning it on gives every table its full period.
    Expired tables are dropped by the worker's clean-up and can be restored with `lakana table
    undrop` during the undo period. Off unless set. Needs MANAGE_ENVIRONMENT; refused for a
    protected environment, whose tables never expire.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    days
        `--days`.
    off
        `--off`.
    yes
        Skip the confirmation prompt of `--days`.

    Raises
    ------
    CliError
        With both options (exit 2).
    """
    state = state_of(ctx)
    environments = state.client().environments
    if days is not None and off:
        raise CliError("Give --days or --off, not both.")
    if days is None and not off:
        shown = environments.get(name)
        if state.output == "json":
            _output.emit(state, shown)
            return
        _output.emit(state, _retention_line(shown.name, shown.retention_days, now=False))
        return
    if days is not None:
        confirm(
            state,
            f"Let every table in {name!r} that sets no retention of its own expire {days} days "
            "after its last write or read?",
            yes,
        )
    changed = environments.set_retention(name, days)
    _output.done(state, _retention_line(changed.name, changed.retention_days, now=True), changed)


def _retention_line(name: str, days: int | None, *, now: bool) -> str:
    """Say what an environment's retention does.

    Parameters
    ----------
    name
        The environment.
    days
        Its retention, or `None` for off.
    now
        Whether it was just changed.

    Returns
    -------
    str
        One sentence.
    """
    shown = _output.inline(name)
    if days is None:
        return f"{shown} has no retention: its tables expire only if they set a retention."
    verb = "now expire" if now else "expire"
    return (
        f"Tables in {shown} that set no retention of their own {verb} {days} days after their "
        "last write or read. See what expires soon with: lakana table expiring --env "
        f"{shown}"
    )


@env_app.command("orphaned-locations")
def orphaned_locations(
    ctx: typer.Context,
    name: str,
    older_than: Annotated[
        str,
        typer.Option(
            "--older-than",
            help="Only directories whose newest file is older than this many days, like 30d.",
        ),
    ] = "30d",
    delete: Annotated[
        bool,
        typer.Option("--delete", help="Delete the directories listed as old enough."),
    ] = False,
    yes: _YES = False,
) -> None:
    """List, or with --delete remove, table directories older releases left without a record.

    Releases before location records could leave table directories under `managed/` that no
    table and no record names: tables dropped before the upgrade, creates that failed,
    environments deleted before their tables could be restored. The worker's clean-up only
    counts them. Without `--delete` this is a dry run: it lists each directory with its files,
    size and newest file, and deletes nothing. A directory with a file newer than
    `--older-than` (30 days by default) is never deleted. `--delete` asks first (`--yes`
    skips the question), checks every directory again, and keeps one that a table, a create
    or a dropped table now uses. NAME may be a deleted environment. Needs MANAGE_ENVIRONMENT
    on the whole instance (the ADMIN role), because the storage root may be shared. After
    restoring the catalog from a backup, keep `--older-than` above the backup's age: a table
    created after the backup has no record either.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment.
    older_than
        `--older-than`, in days.
    delete
        `--delete`.
    yes
        Skip the confirmation prompt of `--delete`.
    """
    state = state_of(ctx)
    days = parse_within(older_than, "--older-than", 30)
    environments = state.client().environments
    found = environments.orphaned_locations(name, older_than_days=days)
    old = [entry for entry in found.locations if not entry.recent and entry.kept_because is None]
    if not delete:
        if state.output == "json":
            _output.emit(state, found)
            return
        _show_listing(state, found, old, days)
        return
    if not old:
        _output.done(state, _nothing_old(found, days), found)
        _cut(state, found)
        return
    confirm(
        state,
        f"Delete {len(old)} table directories without a location record in {name!r}'s storage "
        f"({_amount(old)})? This cannot be undone.",
        yes,
    )
    report = environments.delete_orphaned_locations(name, older_than_days=days)
    deleted = [entry for entry in report.locations if entry.deleted]
    if state.output != "json":
        _output.emit(
            state,
            report.locations,
            columns=["location", "files_deleted", "bytes_deleted", "result"],
            derived={"result": lambda entry: "deleted" if entry.deleted else entry.kept_because},
            flexible=["result"],
        )
    files = sum(entry.files_deleted for entry in report.locations)
    size = sum(entry.bytes_deleted for entry in report.locations)
    _output.done(
        state,
        f"Deleted {_directories(len(deleted))} ({files} files, {human_size(size)}); "
        f"{len(report.locations) - len(deleted)} kept.",
        report,
    )
    if not report.complete:
        _output.next_step(
            state,
            "The server stopped at its time limit with work left; run the same command again "
            "to continue.",
        )


def _directories(count: int) -> str:
    """Count directories in words.

    Parameters
    ----------
    count
        How many.

    Returns
    -------
    str
        Such as ``1 directory`` or ``2 directories``.
    """
    return f"{count} directory" if count == 1 else f"{count} directories"


def _amount(entries: list[OrphanedLocation]) -> str:
    """Say how many files and bytes some directories hold.

    Parameters
    ----------
    entries
        The directories.

    Returns
    -------
    str
        Such as ``3 files, 2.0 KiB``.
    """
    files = sum(entry.files for entry in entries)
    return f"{files} files, {human_size(sum(entry.bytes for entry in entries))}"


def _nothing_old(found: OrphanedLocations, days: int) -> str:
    """Say that a listing found nothing to delete.

    Parameters
    ----------
    found
        The listing.
    days
        `--older-than`.

    Returns
    -------
    str
        One sentence.
    """
    if not found.locations:
        return (
            "No table directories without a location record in "
            f"{_output.inline(found.environment)}'s storage."
        )
    return (
        f"Nothing is old enough to delete: every directory listed has a file newer than {days} "
        "days."
    )


def _show_listing(
    state: CliState, found: OrphanedLocations, old: list[OrphanedLocation], days: int
) -> None:
    """Print a dry run: the directories, a total, and the command that deletes.

    Parameters
    ----------
    state
        The CLI state.
    found
        The listing.
    old
        Its directories that are old enough to delete.
    days
        `--older-than`.
    """
    if not found.locations:
        _output.done(state, _nothing_old(found, days))
        _cut(state, found)
        return
    _output.emit(
        state,
        found.locations,
        columns=["location", "files", "bytes", "newest_file_at", "recent"]
        + (["kept_because"] if any(entry.kept_because for entry in found.locations) else []),
        flexible=["kept_because"],
    )
    total = (
        f"{_directories(len(found.locations))} without a location record "
        f"({_amount(found.locations)})."
    )
    recent = sum(1 for entry in found.locations if entry.recent and not entry.kept_because)
    if recent:
        verb = ("has", "is") if recent == 1 else ("have", "are")
        total += f" {recent} {verb[0]} a file newer than {days} days and {verb[1]} kept."
    named = len(found.locations) - len(old) - recent
    if named:
        total += f" {named} {'is' if named == 1 else 'are'} kept for the reason shown."
    if old:
        shown = _output.inline(found.environment)
        total += (
            f" Delete the other {len(old)} ({_amount(old)}) with: lakana env orphaned-locations "
            f"{shown} --older-than {days}d --delete"
            if recent or named
            else f" Delete them with: lakana env orphaned-locations {shown} --older-than "
            f"{days}d --delete"
        )
    else:
        total += " " + _nothing_old(found, days)
    _output.done(state, total)
    _cut(state, found)


def _cut(state: CliState, found: OrphanedLocations) -> None:
    """Tell the reader when the server's time limit cut a listing short.

    Parameters
    ----------
    state
        The CLI state.
    found
        The listing.
    """
    if not found.complete:
        _output.next_step(
            state,
            "The server stopped at its time limit, so the list may be incomplete; run the "
            "command again, after a --delete, to see more.",
        )


_USAGE_COLUMNS = ["environment", "tables", "dropped", "artifacts", "code", "total"]
_UNMEASURED_NOTE = "A total with + is a floor: the worker's next sweep (hourly) measures the rest."
_CACHE_COLUMNS = ["id", "environments", "packages", "tasks", "total", "measured_at"]
_PACKAGES_NOTE = "packages leaves out files shared with an environment; + marks a floor."
_SHARED_NOTE = "Workers that share one cache volume show the same figures: do not add them up."


def _part(part: UsagePart) -> str:
    """Write one part of a usage for a table cell.

    Parameters
    ----------
    part
        The part.

    Returns
    -------
    str
        ``3.0 GiB in 2``, with ``, N unmeasured`` when some sizes are missing.
    """
    text = f"{human_size(part.bytes)} in {part.count}"
    return f"{text}, {part.unmeasured} unmeasured" if part.unmeasured else text


def cache_size_cell(worker: WorkerInfo, field: str) -> str:
    """Write one cache size of a worker.

    Parameters
    ----------
    worker
        The worker.
    field
        The attribute of its `WorkerCaches`: ``environments_bytes``, ``packages_bytes``,
        ``tasks_bytes`` or ``total_bytes``.

    Returns
    -------
    str
        The size, ``+`` after a measurement cut short, ``-`` before the first one.
    """
    caches = worker.caches
    if caches is None:
        return "-"
    shown = human_size(getattr(caches, field))
    return shown if caches.complete else shown + "+"


def _usage_rows(state: CliState, name: str | None) -> tuple[list[EnvironmentUsage], list[str]]:
    """Read the usage of one environment, or of every one the caller may manage.

    Parameters
    ----------
    state
        The CLI state.
    name
        The environment; `None` for every one the caller sees.

    Returns
    -------
    tuple
        The usages, and the names of the environments the caller sees but may not manage.

    Raises
    ------
    CliError
        If no environment is left to show.
    """
    client = state.client()
    if name is not None:
        return [client.environments.usage(name)], []
    found: list[EnvironmentUsage] = []
    refused: list[str] = []
    for environment in client.environments.list():
        try:
            found.append(client.environments.usage(environment.name))
        except PermissionDenied:
            refused.append(environment.name)
    if not found:
        raise CliError(
            "There is no environment whose storage you may see: the usage needs "
            "MANAGE_ENVIRONMENT on the environment. Ask an administrator for the grant, or name "
            "an environment you manage: lakana env usage NAME.",
            exit_code=1,
        )
    return found, refused


@env_app.command("usage")
def usage(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(help="The environment; every environment you manage when left out."),
    ] = None,
) -> None:
    """Show how much storage environments use, and the size of the workers' caches.

    For each environment: its managed tables (the files of their current version), the dropped
    tables that can still be restored, its artifacts and its code archives. The figures come
    from the catalog database; every write stores its table's size, and the worker reads the
    size of a table written before that in its hourly sweep. A table whose write did not state
    its size stays unmeasured until a write states it.
    Not counted: the files of older table versions
    that maintenance has not removed yet, external tables, and files nothing names. Needs
    MANAGE_ENVIRONMENT on each environment; the workers' caches need it on the instance.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The environment, or `None` for every one the caller may manage.
    """
    state = state_of(ctx)
    usages, refused = _usage_rows(state, name)
    try:
        workers: list[WorkerInfo] | None = state.client().workers.list()
    except PermissionDenied:
        workers = None
    if state.output == "json":
        _output.print_json(
            {
                "environments": [asdict(found) for found in usages],
                "workers": None if workers is None else [asdict(w) for w in workers],
            }
        )
        return
    _output.emit(
        state,
        usages,
        columns=_USAGE_COLUMNS,
        derived={
            "tables": lambda u: _part(u.tables),
            "dropped": lambda u: _part(u.dropped_tables),
            "artifacts": lambda u: _part(u.artifacts),
            "code": lambda u: _part(u.code_archives),
            "total": lambda u: human_size(u.total_bytes) + ("" if u.complete else "+"),
        },
        whole=["total"],
        footnotes=None if all(u.complete for u in usages) else {"total": _UNMEASURED_NOTE},
    )
    if refused:
        typer.echo(
            f"{len(refused)} environment{'s' if len(refused) > 1 else ''} not shown "
            f"({', '.join(_output.inline(n) for n in refused)}): you may not manage "
            f"{'them' if len(refused) > 1 else 'it'}."
        )
    typer.echo("")
    if workers is None:
        typer.echo(
            "The workers' caches are shown to administrators of the instance "
            "(MANAGE_ENVIRONMENT on the instance)."
        )
        return
    if not workers:
        typer.echo("No worker is registered, so no cache is shown.")
        return
    _output.emit(
        state,
        workers,
        columns=_CACHE_COLUMNS,
        derived={
            "environments": lambda w: cache_size_cell(w, "environments_bytes"),
            "packages": lambda w: cache_size_cell(w, "packages_bytes"),
            "tasks": lambda w: cache_size_cell(w, "tasks_bytes"),
            "total": lambda w: cache_size_cell(w, "total_bytes"),
            "measured_at": lambda w: (
                "not measured yet"
                if w.caches is None
                else w.caches.measured_at.isoformat(timespec="seconds")
            ),
        },
        whole=["total", "measured_at"],
        footnotes={"packages": _PACKAGES_NOTE, "id": _SHARED_NOTE},
    )


def register(app: typer.Typer) -> None:
    """Add the `env` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(env_app, name="env")
