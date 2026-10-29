"""`lakana deploy`, `lakana rollback`, and the deploy read commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from lakana._models import Deploy, DeployJobStart, ProjectVersion
from lakana.cli import _output
from lakana.cli._paging import more_results
from lakana.cli._project import find_project_root, is_dirty, project_repo
from lakana.cli._state import CliError, CliState, confirm, resolve_environment, state_of
from lakana.cli._waiting import WaitLine, clock
from lakana.client import Client
from lakana.errors import LakanaError, NotFound

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_REPO = Annotated[
    str | None, typer.Argument(help="Repository name (default: lakana.yaml's repo:).")
]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_TIMEOUT = Annotated[float | None, typer.Option("--timeout", help="Seconds to wait.")]
_GROUP_ENV = "lakana.deployments.env"
_MOVED_HINT = "'lakana deploy init' is now 'lakana bundle init'"

deploys_app = typer.Typer(help="Deploy history.", no_args_is_help=True)
deployments_app = typer.Typer(
    help="Current deployments and the identity each one runs as.", invoke_without_command=True
)
project_version_app = typer.Typer(help="Immutable project versions.", no_args_is_help=True)


_JOB_CHANGES = {"jobs_added": "added", "jobs_removed": "removed", "jobs_changed": "changed"}
"""The diff categories about jobs, which are not tables, and the word each is printed with."""

_NOT_TABLES = frozenset({*_JOB_CHANGES, "hints"})
"""The diff keys that do not list changed tables: the job categories and the deploy's hints."""


@dataclass(frozen=True)
class DiffRow:
    """One changed table in a deploy diff.

    Parameters
    ----------
    change
        The category.
    table
        The table.
    """

    change: str
    table: str


@dataclass(frozen=True)
class AssetRow:
    """One asset of a project version.

    Parameters
    ----------
    table
        Dotted table name.
    materialize
        Materialization.
    owner
        Owner.
    sql
        ``shown`` or ``hidden (N references)``.
    """

    table: str
    materialize: str
    owner: str | None
    sql: str


def resolve_repository(repo: str | None) -> str:
    """Resolve the repository argument.

    Parameters
    ----------
    repo
        The positional argument.

    Returns
    -------
    str
        It, or `lakana.yaml`'s `repo:`.

    Raises
    ------
    CliError
        If neither names one (exit 2).
    """
    name = repo or project_repo(Path.cwd())
    if name is None:
        raise CliError("Name a repository, or run from a project whose lakana.yaml sets repo:")
    return name


def _warn_if_dirty(state: CliState, repository: str) -> None:
    """Warn when a local repository's work tree here has uncommitted changes.

    Parameters
    ----------
    state
        The CLI state.
    repository
        The repository being deployed.
    """
    root = find_project_root(Path.cwd())
    if root is None or not is_dirty(root):
        return
    try:
        kind = state.client().repositories.get(repository).kind
    except LakanaError:
        return
    if kind == "local":
        typer.echo(
            f"warning: {root} has uncommitted changes; only the committed state is deployed.",
            err=True,
        )


def _report(state: CliState, deploy: Deploy, verb: str) -> None:
    """Print a finished run: SHA and diff, then its hints and warnings; or the run as JSON.

    Parameters
    ----------
    state
        The CLI state.
    deploy
        The finished run.
    verb
        ``Deployed``, ``Dry run of`` or ``Rolled back``.
    """
    if state.output == "json":
        _output.print_json(deploy)
        return
    typer.echo(f"{verb} {deploy.repository} in {deploy.environment} at {deploy.resolved_sha}")
    diff = deploy.diff or {}
    rows = [
        DiffRow(change=change, table=table)
        for change, tables in diff.items()
        if change not in _NOT_TABLES
        for table in tables
    ]
    jobs = [
        f"  {word} {_output.inline(name)}"
        for change, word in _JOB_CHANGES.items()
        for name in diff.get(change, ())
    ]
    if rows:
        _output.emit(state, rows)
        if deploy.hidden_adopted:
            typer.echo(f"({deploy.hidden_adopted} adopted table(s) hidden: you can't read them)")
    elif deploy.hidden_adopted:
        typer.echo(
            f"No visible changes; {deploy.hidden_adopted} adopted table(s) hidden "
            "(you can't read them)."
        )
    elif not jobs:
        typer.echo("No changes.")
    if jobs:
        typer.echo("Jobs:")
        for line in jobs:
            typer.echo(line)
    if deploy.checks:
        typer.echo("Checks:")
        for table, checks in deploy.checks.items():
            names = ", ".join(
                _output.inline(check["check"])
                + (" (generated)" if "generated" in check else "")
                + (f" ({check['on_failure']})" if check["on_failure"] in {"keep", "warn"} else "")
                for check in checks
            )
            typer.echo(f"  {_output.inline(table)}: {names}")
        defaulted = [
            check for checks in deploy.checks.values() for check in checks if "from" in check
        ]
        if defaulted:
            value = _output.inline(defaulted[0]["on_failure"])
            takes = "check takes" if len(defaulted) == 1 else "checks take"
            typer.echo(f"{len(defaulted)} {takes} `on_failure: {value}` from lakana.yaml.")
    accepted = [start for start in deploy.job_starts if start.outcome in _ACCEPTED]
    if accepted:
        typer.echo("Jobs started after the deploy:")
        for start in accepted:
            typer.echo(f"  {_output.inline(start.job)}: {_started(start)}")
    for hint in diff.get("hints", ()):
        typer.echo(f"hint: {_output.inline(hint, 500)}", err=True)
    for warning in deploy.warnings:
        typer.echo(f"warning: {_output.inline(warning, 500)}", err=True)
    for start in deploy.job_starts:
        if start.outcome not in _ACCEPTED:
            text = start.message or f"job `{start.job}` was not started ({start.outcome})"
            typer.echo(f"warning: {_output.inline(text, 500)}", err=True)


_ACCEPTED = frozenset({"started", "waiting"})
"""The outcomes of a job start that need nothing from the person who deployed."""


def _started(start: DeployJobStart) -> str:
    """Say what came of a job start the deploy made.

    Parameters
    ----------
    start
        A ``started`` or ``waiting`` start.

    Returns
    -------
    str
        ``run ID``, or ``waits for run ID; it starts then``.
    """
    if start.outcome == "started":
        return f"run {start.run_id}"
    return f"waits for run {start.waits_for_run_id}; it starts then"


def _wait(
    state: CliState, client: Client, environment: str, deploy_id: str, timeout: float | None
) -> Deploy:
    """Wait for a run, turning a timeout into a message with the id to poll.

    While the run is queued, a line on standard error says why (see `WaitLine`).

    Parameters
    ----------
    state
        The CLI state (for the output mode).
    client
        The SDK client.
    environment
        Environment name.
    deploy_id
        The run's id.
    timeout
        Seconds to wait, or `None`.

    Returns
    -------
    Deploy
        The succeeded run.

    Raises
    ------
    typer.Exit
        With code 1 if `timeout` elapses first; the run keeps going server-side.
    """
    try:
        return client.deploys.wait(
            environment, deploy_id, timeout=timeout, on_update=WaitLine(state, monotonic=clock)
        )
    except TimeoutError as exc:
        typer.echo(
            f"error: {exc}. The deploy is still in progress; check it with "
            f"'lakana deploys show {deploy_id} --env {environment}'.",
            err=True,
        )
        raise typer.Exit(1) from exc


def register(app: typer.Typer) -> None:
    """Add `deploy`, `rollback`, `deploys`, `deployments`, `undeploy`, `project-version`, `orphans`.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("deploy")(deploy)
    app.command("rollback")(rollback)
    app.add_typer(deploys_app, name="deploys")
    app.add_typer(deployments_app, name="deployments")
    app.command("undeploy")(undeploy)
    app.add_typer(project_version_app, name="project-version")
    app.command("orphans")(orphans)


def deploy(
    ctx: typer.Context,
    repo: _REPO = None,
    env: _ENV = None,
    ref: Annotated[str | None, typer.Option("--ref", help="Branch, tag or full SHA.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and diff only.")] = False,
    no_wait: Annotated[
        bool, typer.Option("--no-wait", help="Print the deploy id and exit.")
    ] = False,
    timeout: _TIMEOUT = None,
) -> None:
    """Deploy REPO to an environment; exit 1 if the deploy fails.

    In a CI job, LAKANA_OIDC=github or LAKANA_OIDC=gitlab (or LAKANA_OIDC_TOKEN) exchanges the
    job's OIDC ID token for a one-deploy CI token instead of reading a stored token.

    Parameters
    ----------
    ctx
        The Typer context.
    repo
        Repository name.
    env
        Environment.
    ref
        Ref to deploy.
    dry_run
        Validate and diff only.
    no_wait
        Return immediately.
    timeout
        Seconds to wait.

    Raises
    ------
    CliError
        Re-raised with a hint naming `lakana bundle init` when REPO is `init`
        and the environment cannot be resolved or no such repository exists.
    NotFound
        Re-raised unchanged for any other repository.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    try:
        environment = resolve_environment(state, env)
        state.use_ci_oidc(environment, repository)
        _warn_if_dirty(state, repository)
        client = state.client()
        accepted = client.deploys.create(
            environment, repository, kind="dry_run" if dry_run else "deploy", ref=ref
        )
    except (CliError, NotFound) as exc:
        if repository != "init":
            raise
        # `lakana deploy init` used to generate the Compose bundle.
        if isinstance(exc, NotFound):
            raise CliError(str(exc), 6, hint=_MOVED_HINT, kind="NotFound") from exc
        hint = "; ".join(part for part in (exc.hint, _MOVED_HINT) if part)
        raise CliError(str(exc), exc.exit_code, hint=hint, kind=exc.kind) from exc
    if not no_wait:
        typer.echo(f"Queued {'dry run' if dry_run else 'deploy'} {accepted.id}", err=True)
    if no_wait:
        if state.output == "json":
            _output.print_json(accepted)
        else:
            typer.echo(accepted.id)
        return
    finished = _wait(state, client, environment, accepted.id, timeout)
    _report(state, finished, "Dry run of" if dry_run else "Deployed")
    if any(start.outcome in _ACCEPTED for start in finished.job_starts):
        if state.output != "json":
            typer.echo(
                f"Hint: follow a run with 'lakana runs show RUN_ID --env {environment}'.", err=True
            )
    elif not dry_run and state.output != "json":
        # A repository named on the command line must be named again: the short form only
        # works from inside the project.
        target = f"{repo} " if repo else ""
        typer.echo(
            f"Hint: run 'lakana run {target}--env {environment}' to run what you just deployed.",
            err=True,
        )


def rollback(
    ctx: typer.Context,
    repo: _REPO = None,
    env: _ENV = None,
    to: Annotated[str | None, typer.Option("--to", help="A full SHA deployed here before.")] = None,
    to_previous: Annotated[
        bool, typer.Option("--to-previous", help="The project version current before this one.")
    ] = False,
    timeout: _TIMEOUT = None,
) -> None:
    """Move REPO's deployment back without fetching from git.

    Parameters
    ----------
    ctx
        The Typer context.
    repo
        Repository name.
    env
        Environment.
    to
        Target SHA.
    to_previous
        Return to the previous project version.
    timeout
        Seconds to wait.

    Raises
    ------
    CliError
        Unless exactly one of `--to` / `--to-previous` is given (exit 2).
    """
    state = state_of(ctx)
    if (to is None) == (not to_previous):
        raise CliError("Pass exactly one of --to SHA or --to-previous")
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    client = state.client()
    accepted = client.deploys.create(
        environment, repository, kind="rollback", to_sha=to, to_previous=to_previous
    )
    typer.echo(f"Queued rollback {accepted.id}", err=True)
    _report(state, _wait(state, client, environment, accepted.id, timeout), "Rolled back")


@deploys_app.command("list")
def list_deploys(
    ctx: typer.Context,
    env: _ENV = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Only this repository.")] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, max=200, help="Most deploys to list (default 100)."),
    ] = None,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Continue after the page that printed this cursor."),
    ] = None,
) -> None:
    """List deploy history, newest first.

    A long list prints a "More results" line with the command that shows the next page.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    repo
        Repository filter.
    limit
        Most deploys returned (1 to 200); the server's default when omitted.
    cursor
        The cursor of the page to show.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    page = state.client().deploys.list_page(
        environment, repository=repo, limit=limit, cursor=cursor
    )
    _output.emit(
        state,
        page.items,
        columns=["id", "repository", "kind", "status", "resolved_sha", "created_at"],
        droppable=["kind", "resolved_sha", "repository", "created_at"],
    )
    more_results(
        state,
        ["lakana", "deploys", "list"],
        {"--env": environment, "--repo": repo, "--limit": limit},
        page.next_cursor,
    )


@deploys_app.command("show")
def show_deploy(ctx: typer.Context, deploy_id: str, env: _ENV = None) -> None:
    """Show deploy DEPLOY_ID.

    Parameters
    ----------
    ctx
        The Typer context.
    deploy_id
        The run's id.
    env
        Environment.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().deploys.get(resolve_environment(state, env), deploy_id))


def _list_deployments(ctx: typer.Context, env: str | None) -> None:
    """Print the current project version and the identity it runs as, per repository.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    """
    state = state_of(ctx)
    env = env or ctx.meta.get(_GROUP_ENV)
    rows = state.client().deployments.list(resolve_environment(state, env))
    _output.emit(
        state,
        rows,
        columns=["repository", "commit_sha", "run_as", "project_version_id", "updated_at"],
        droppable=["project_version_id", "run_as", "updated_at", "commit_sha"],
    )


@deployments_app.callback()
def deployments(ctx: typer.Context, env: _ENV = None) -> None:
    """Show the current project version per repository (also `deployments list`).

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    """
    ctx.meta[_GROUP_ENV] = env
    if ctx.invoked_subcommand is None:
        _list_deployments(ctx, env)


@deployments_app.command("list")
def list_deployments(ctx: typer.Context, env: _ENV = None) -> None:
    """Show the current project version per repository.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    """
    _list_deployments(ctx, env)


@deployments_app.command("set-run-as")
def set_run_as(
    ctx: typer.Context,
    repo: _REPO = None,
    env: _ENV = None,
    principal: Annotated[
        str | None, typer.Option("--principal", help="An active service account.")
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Stop using a fixed identity for the runs.")
    ] = False,
) -> None:
    """Bind (or with --clear remove) the service account REPO's runs write as.

    Parameters
    ----------
    ctx
        The Typer context.
    repo
        Repository name.
    env
        Environment.
    principal
        The service account.
    clear
        Stop using a fixed identity instead.

    Raises
    ------
    CliError
        Unless exactly one of `--principal` / `--clear` is given (exit 2).
    """
    state = state_of(ctx)
    if (principal is None) == (not clear):
        raise CliError("Pass exactly one of --principal NAME or --clear")
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env or ctx.meta.get(_GROUP_ENV))
    client = state.client()
    if principal is not None:
        view = client.deployments.set_run_as(environment, repository, principal)
    else:
        view = client.deployments.clear_run_as(environment, repository)
    _output.emit(state, view)


def undeploy(ctx: typer.Context, repo: _REPO = None, env: _ENV = None, yes: _YES = False) -> None:
    """Remove REPO's deployment and the identity its runs use; tables and history stay.

    Parameters
    ----------
    ctx
        The Typer context.
    repo
        Repository name.
    env
        Environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    confirm(state, f"Undeploy {repository} from {environment}?", yes)
    state.client().deployments.undeploy(environment, repository)
    _output.done(
        state, f"Undeployed {_output.inline(repository)} from {_output.inline(environment)}"
    )


def dag_lines(manifest: dict) -> list[str]:
    """Render a manifest's DAG as tree lines, roots first.

    Parameters
    ----------
    manifest
        A project version manifest.

    Returns
    -------
    list of str
        Indented lines; a node reached twice prints ``(see above)`` the second time.
    """
    tables = [asset["table"] for asset in manifest.get("assets", [])]
    downstream: dict[str, list[str]] = {table: [] for table in tables}
    has_upstream: set[str] = set()
    for upstream, target in manifest.get("edges", []):
        downstream.setdefault(upstream, []).append(target)
        has_upstream.add(target)
    lines: list[str] = []
    seen: set[str] = set()

    def walk(table: str, depth: int) -> None:
        """Append `table` and its downstream assets.

        Parameters
        ----------
        table
            The node.
        depth
            Indentation level.
        """
        prefix = "   " * (depth - 1) + "└─ " if depth else ""
        if table in seen:
            lines.append(f"{prefix}{table} (see above)")
            return
        seen.add(table)
        lines.append(f"{prefix}{table}")
        for child in sorted(downstream.get(table, [])):
            walk(child, depth + 1)

    for root in sorted(t for t in tables if t not in has_upstream):
        walk(root, 0)
    return lines


_MAX_INDEX_LINES = 20
"""The most package indexes `project-version show` lists; `--output json` has every one."""


@project_version_app.command("show")
def show_project_version(ctx: typer.Context, project_version_id: str, env: _ENV = None) -> None:
    """Show project version ID: its assets, the package indexes its lock uses and the DAG as a tree.

    Parameters
    ----------
    ctx
        The Typer context.
    project_version_id
        The project version.
    env
        Environment.
    """
    state = state_of(ctx)
    version: ProjectVersion = state.client().project_versions.get(
        resolve_environment(state, env), project_version_id
    )
    if state.output == "json":
        _output.print_json(version)
        return
    typer.echo(f"{version.repository} @ {version.commit_sha} in {version.environment}")
    typer.echo(f"code: {version.code_uri} (sha256 {version.code_sha256})")
    indexes = version.manifest.get("package_indexes")
    if isinstance(indexes, list) and indexes and all(isinstance(name, str) for name in indexes):
        typer.echo("package indexes:")
        for name in indexes[:_MAX_INDEX_LINES]:
            # One per line, escaped: a name may hold a comma, an escape sequence or a line break.
            typer.echo(f"  - {_output.inline(name)}")
        if len(indexes) > _MAX_INDEX_LINES:
            typer.echo(f"  and {len(indexes) - _MAX_INDEX_LINES} more: use --output json")
    rows = [
        AssetRow(
            table=asset["table"],
            materialize=asset.get("materialize", ""),
            owner=(asset.get("metadata") or {}).get("owner"),
            sql=(
                f"hidden ({asset['hidden_references']} references)"
                if asset.get("hidden_references")
                else "shown"
            ),
        )
        for asset in version.manifest.get("assets", [])
    ]
    _output.emit(state, rows)
    for line in dag_lines(version.manifest):
        typer.echo(line)
    hidden = version.manifest.get("hidden_external_inputs", 0)
    if hidden:
        typer.echo(f"{hidden} external input(s) hidden")
    hidden_edges = version.manifest.get("hidden_edges", 0)
    if hidden_edges:
        typer.echo(f"{hidden_edges} edge(s) hidden")


def orphans(ctx: typer.Context, env: _ENV = None) -> None:
    """List tables earlier project versions produced that no current one does.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    """
    state = state_of(ctx)
    report = state.client().orphans.list(resolve_environment(state, env))
    if state.output == "json":
        _output.print_json(report)
        return
    _output.emit(state, report.orphans)
    if report.hidden:
        typer.echo(f"{report.hidden} hidden")
