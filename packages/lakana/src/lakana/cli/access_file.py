"""`lakana access export`, `plan`, `apply` and `status`: access as a file (#46).

The file is read by the server; the CLI only checks its size, so a file that cannot be
accepted is refused before it is sent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from lakana._models import AccessPlan
from lakana.cli import _output
from lakana.cli._state import CliError, confirm, state_of

MAX_FILE_BYTES = 1024 * 1024
"""The most an access file may hold; the server refuses a larger request too."""

_REFERENCE = "The file format: https://lakana-labs.github.io/lakana/reference/access-file/."

_SIGNS = {"create": "+", "change": "~", "remove": "-"}


def _read_file(path: Path) -> str:
    """Read an access file, refusing one the server could not accept.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    str
        Its text.

    Raises
    ------
    CliError
        If it cannot be read, is larger than 1 MiB (as a file or as the request that sends it),
        or is not UTF-8 (exit 1).
    """
    name = _output.inline(str(path))
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise CliError(
                f"{name} is {size} bytes, larger than {MAX_FILE_BYTES} bytes, the most an access "
                "file may hold. Use a group instead of many people, or remove entries that no "
                "longer apply.",
                1,
            )
        data = path.read_bytes()
    except OSError as exc:
        raise CliError(
            f"Cannot read {name}: {exc.strerror}. Write a file to start from with "
            "`lakana access export -o access.yaml`.",
            1,
        ) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(
            f"{name} is not UTF-8 text (byte {exc.start}). Save it as UTF-8 and try again.", 1
        ) from exc
    body = json.dumps(
        {"file_name": path.name, "text": text, "allow_lockout": False},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if len(body) > MAX_FILE_BYTES:
        raise CliError(
            f"The request for {name} would be {len(body)} bytes once the text is escaped for "
            f"sending, larger than the {MAX_FILE_BYTES} bytes the server accepts. Remove entries "
            "that no longer apply, or manage fewer scopes in this file: "
            "`lakana access export --scope ENV` writes one environment.",
            1,
        )
    return text


def _drift_line(scope: str, text: str, by: str | None, reason: str | None, at: str | None) -> str:
    """Write one drift line.

    Parameters
    ----------
    scope
        The scope.
    text
        What it is and what an apply does with it.
    by
        Who made it, when known.
    reason
        Why, when known.
    at
        When, when known.

    Returns
    -------
    str
        ``! drift in prod: …``, with ``— by, at: reason`` when something is known.
    """
    line = f"! drift in {_output.inline(scope)}: {_output.inline(text, 500)}"
    who = ", ".join(_output.inline(part) for part in (by, at) if part)
    why = f": {_output.inline(reason, 500)}" if reason else ""
    if who or why:
        line += f" — {who}{why}"
    return line


def _print_plan(plan: AccessPlan) -> None:
    """Print a plan: each change, drift, releases, then the fingerprint.

    Problems are not printed here; `plan` raises them as an error.

    Parameters
    ----------
    plan
        The plan.
    """
    changes = [*plan.creates, *plan.changed, *plan.removals]
    for change in changes:
        line = f"{_SIGNS[change.op]} {_output.inline(change.text, 500)}"
        if change.entry:
            line += f"  ({_output.inline(change.entry)})"
        typer.echo(line)
    for drift in plan.drift:
        at = None if drift.at is None else drift.at.isoformat()
        typer.echo(_drift_line(drift.scope, drift.text, drift.by, drift.reason, at))
    for scope in plan.releases:
        typer.echo(f"stops managing {_output.inline(scope)}")
    if not changes and not plan.releases:
        typer.echo("No changes.")
    typer.echo(f"Fingerprint: {plan.fingerprint}")


def export(
    ctx: typer.Context,
    scope: Annotated[
        list[str] | None,
        typer.Option(
            "--scope",
            help="`instance` (the default) or an environment; repeat it for several environments.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output-file", "-o", help="Write the file here instead of printing it."),
    ] = None,
) -> None:
    """Write the live access as an access file.

    The file lists the groups, service accounts, grants, row filters, column masks and CI
    trust rules of the scopes, in the format `lakana access plan` reads; planning it unedited
    plans nothing. Needs the ADMIN role on the instance.

    The file format: https://lakana-labs.github.io/lakana/reference/access-file/.

    Parameters
    ----------
    ctx
        The Typer context.
    scope
        The scopes, or the instance when omitted.
    output
        Where to write the file; stdout when omitted.

    Raises
    ------
    CliError
        If the file cannot be written (exit 1).
    """
    state = state_of(ctx)
    text = state.client().access.export(scope or ["instance"])
    if output is None:
        typer.echo(text, nl=False)
        return
    try:
        output.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise CliError(
            f"Cannot write {_output.inline(str(output))}: {exc.strerror}. Choose a folder that "
            "exists, or leave out -o to print the file.",
            1,
        ) from exc
    typer.echo(f"Wrote {_output.inline(str(output))}.")


def plan(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="The access file, such as access.yaml.")],
    allow_lockout: Annotated[
        bool,
        typer.Option(
            "--allow-lockout",
            help="Accept a file that drops your own ADMIN role while another administrator "
            "remains.",
        ),
    ] = False,
) -> None:
    """Show what applying an access file would change, and every problem that stops it.

    Nothing is changed. Prints `+` for what would be created, `~` for a CI trust rule changed
    in place and `-` for what would be removed, then access in the file's scopes that did not
    come from it (drift). Exits 1, with every problem on stderr, when the file has any. Needs
    the ADMIN role on the instance.

    The file format: https://lakana-labs.github.io/lakana/reference/access-file/.

    Parameters
    ----------
    ctx
        The Typer context.
    file
        The access file.
    allow_lockout
        Accept a file that drops the caller's own ADMIN role.

    Raises
    ------
    CliError
        If the file cannot be read or is too large, or the plan has problems (exit 1).
    typer.Exit
        With code 1 after printing the plan as JSON when it has problems.
    """
    state = state_of(ctx)
    answer = state.client().access.plan(
        _read_file(file), file_name=file.name, allow_lockout=allow_lockout
    )
    if state.output == "json":
        _output.print_json(answer)
        if answer.problems:
            raise typer.Exit(1)
        return
    _print_plan(answer)
    if answer.problems:
        raise CliError(
            "\n".join(_output.safe_text(problem) for problem in answer.problems)
            + f"\nFix them and plan again. {_REFERENCE}",
            1,
        )


def apply(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="The access file, such as access.yaml.")],
    plan_fingerprint: Annotated[
        str | None,
        typer.Option(
            "--plan-fingerprint",
            help="The fingerprint `lakana access plan` printed: applies without asking, since "
            "that plan was the review. Without it the command plans first, shows the plan "
            "and asks (the command asks only when it plans itself).",
        ),
    ] = None,
    source_commit: Annotated[
        str | None,
        typer.Option(
            "--source-commit",
            help="The commit the file came from (7 to 64 hex digits), recorded with the apply.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before applying; for CI.")] = False,
    allow_lockout: Annotated[
        bool,
        typer.Option(
            "--allow-lockout",
            help="Accept a file that drops your own ADMIN role while another administrator "
            "remains.",
        ),
    ] = False,
) -> None:
    """Make an access file's changes in one audited transaction, or none of them.

    Without `--plan-fingerprint` the command plans the file, shows the plan and asks before
    it applies exactly that plan. With `--plan-fingerprint` it does not ask: that plan was the
    review. In CI, pass `--yes` and the fingerprint of the plan step:
    the apply is refused, with nothing changed, when the file or the access changed since that
    plan. Needs the ADMIN role on the instance and a personal access token.

    The file format: https://lakana-labs.github.io/lakana/reference/access-file/.

    Parameters
    ----------
    ctx
        The Typer context.
    file
        The access file.
    plan_fingerprint
        The reviewed plan's fingerprint; planned first when omitted.
    source_commit
        The commit the file came from.
    yes
        Skip the question.
    allow_lockout
        Accept a file that drops the caller's own ADMIN role.

    Raises
    ------
    CliError
        If the file cannot be read or is too large, the plan has problems, or there is no
        terminal to ask on and neither `--yes` nor `--plan-fingerprint` was given (exit 1).
    """
    state = state_of(ctx)
    text = _read_file(file)
    access = state.client().access
    fingerprint = plan_fingerprint
    if fingerprint is None:
        reviewed = access.plan(text, file_name=file.name, allow_lockout=allow_lockout)
        if reviewed.problems:
            raise CliError(
                "\n".join(_output.safe_text(problem) for problem in reviewed.problems)
                + f"\nFix them and plan again. {_REFERENCE}",
                1,
            )
        if not yes and not state.is_interactive():
            raise CliError(
                "lakana access apply asks before it changes access; in CI pass --yes, with "
                "--plan-fingerprint from the plan step",
                1,
            )
        if state.output != "json":
            _print_plan(reviewed)
        confirm(state, "Apply these changes?", yes)
        fingerprint = reviewed.fingerprint
    result = access.apply(
        text,
        plan_fingerprint=fingerprint,
        file_name=file.name,
        source_commit=source_commit,
        allow_lockout=allow_lockout,
    )
    if state.output == "json":
        _output.print_json(result)
        return
    typer.echo(
        f"Applied: {result.created} created, {result.changed} changed, {result.removed} removed."
    )
    if result.released:
        typer.echo("Stopped managing: " + ", ".join(_output.inline(x) for x in result.released))


def status(ctx: typer.Context) -> None:
    """Show the scopes access files manage and the changes made outside them.

    Lists each managed scope with the file, commit, administrator and time of its last apply,
    then the overrides no apply has taken into account yet. Needs the ADMIN role on the
    instance.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    answer = state.client().access.status()
    if state.output == "json":
        _output.print_json(answer)
        return
    if not answer.scopes:
        typer.echo(
            "No scope is managed by an access file. Write one with "
            "`lakana access export -o access.yaml`, then `lakana access apply access.yaml`."
        )
    else:
        _output.emit(
            state,
            answer.scopes,
            columns=["scope", "file", "commit", "applied by", "applied at"],
            derived={
                "file": lambda x: x.file_name,
                "commit": lambda x: x.source_commit,
                "applied by": lambda x: x.applied_by,
                "applied at": lambda x: x.applied_at,
            },
            whole=["applied at"],
        )
    if answer.overrides:
        typer.echo("Open overrides (the next apply reverts them unless the file lists them):")
        _output.emit(state, answer.overrides, columns=["scope", "change", "reason", "by", "at"])


def register(app: typer.Typer) -> None:
    """Add the file commands to the `access` group.

    Parameters
    ----------
    app
        The `access` app.
    """
    app.command("export")(export)
    app.command("plan")(plan)
    app.command("apply")(apply)
    app.command("status")(status)
