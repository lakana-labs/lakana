"""`lakana ci-trust *`: which CI jobs may deploy without a stored secret (instance administrators).

A CI trust rule lets a GitHub Actions or GitLab CI job exchange its OIDC ID token for a
short-lived Lakana token that may queue one deploy of the job's own commit. `explain` tests a
CI job's claims against the rules without any token.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, parse_key_values, state_of

ci_trust_app = typer.Typer(
    help="Let CI jobs (GitHub Actions, GitLab CI) deploy with their OIDC ID token, without a "
    "stored token.",
    no_args_is_help=True,
)

_LIST_COLUMNS = ["id", "environment", "repository", "principal", "kinds", "enabled", "conditions"]
_MAX_CLAIMS_BYTES = 64 * 1024


@ci_trust_app.command("create")
def create(
    ctx: typer.Context,
    issuer: Annotated[
        str, typer.Option("--issuer", help="github, gitlab or a configured issuer URL.")
    ],
    env: Annotated[str, typer.Option("--env", help="The environment CI may deploy to.")],
    repo: Annotated[str, typer.Option("--repo", help="The registered repository CI may deploy.")],
    principal: Annotated[
        str,
        typer.Option(
            "--principal", help="The service account the CI job acts as; it needs DEPLOY."
        ),
    ],
    claim: Annotated[
        list[str],
        typer.Option(
            "--claim",
            help="NAME=VALUE the ID token's claim must equal exactly (repeatable). Must include "
            "repository_id (GitHub) or project_id (GitLab), ref or environment, and how the run "
            "was started: event_name or environment (GitHub); ref_protected, pipeline_source or "
            "environment (GitLab).",
        ),
    ],
    kind: Annotated[
        list[str] | None,
        typer.Option("--kind", help="deploy (the default) or dry_run; repeat for both."),
    ] = None,
    start_on_deploy_jobs: Annotated[
        bool,
        typer.Option(
            "--start-on-deploy-jobs",
            help="Let a deploy under this rule start the project's jobs with on_deploy: true "
            "in its environment, as the CI identity (off by default).",
        ),
    ] = False,
) -> None:
    """Create a CI trust rule.

    Parameters
    ----------
    ctx
        The Typer context.
    issuer
        The CI provider.
    env
        The environment.
    repo
        The repository.
    principal
        The service account.
    claim
        The claim conditions.
    kind
        What the rule allows.
    start_on_deploy_jobs
        Whether its deploys start the project's on_deploy jobs.
    """
    state = state_of(ctx)
    rule = state.client().ci_trust_rules.create(
        issuer=issuer,
        principal=principal,
        environment=env,
        repository=repo,
        conditions=parse_key_values(claim, "--claim"),
        kinds=kind or ["deploy"],
        start_on_deploy_jobs=start_on_deploy_jobs,
    )
    starts = " Each deploy starts the deploy's on_deploy jobs." if rule.start_on_deploy_jobs else ""
    _output.done(
        state,
        f"Created CI trust rule {rule.id}: {_output.inline(rule.issuer)} jobs may "
        f"{' and '.join(rule.kinds)} {_output.inline(repo)} to {_output.inline(env)} as "
        f"{_output.inline(principal)}.{starts}\n"
        f"Check that {_output.inline(principal)} holds DEPLOY there: "
        f"lakana access explain {shlex.quote(principal)} --scope {shlex.quote(env)}",
        rule,
    )


@ci_trust_app.command("list")
def list_rules(ctx: typer.Context) -> None:
    """List every CI trust rule, oldest first.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(
        state,
        state.client().ci_trust_rules.list(),
        columns=_LIST_COLUMNS,
        droppable=["conditions", "principal"],
    )


@ci_trust_app.command("show")
def show(ctx: typer.Context, rule_id: str) -> None:
    """Show CI trust rule RULE_ID.

    Parameters
    ----------
    ctx
        The Typer context.
    rule_id
        The rule's id.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().ci_trust_rules.get(rule_id))


@ci_trust_app.command("update")
def update(
    ctx: typer.Context,
    rule_id: str,
    principal: Annotated[
        str | None, typer.Option("--principal", help="A new service account.")
    ] = None,
    claim: Annotated[
        list[str] | None,
        typer.Option(
            "--claim", help="NAME=VALUE (repeatable); replaces all of the rule's conditions."
        ),
    ] = None,
    kind: Annotated[
        list[str] | None, typer.Option("--kind", help="deploy or dry_run (repeatable).")
    ] = None,
    start_on_deploy_jobs: Annotated[
        bool | None,
        typer.Option(
            "--start-on-deploy-jobs/--no-start-on-deploy-jobs",
            help="Whether a deploy under this rule starts the project's on_deploy jobs.",
        ),
    ] = None,
) -> None:
    """Change CI trust rule RULE_ID; the tokens issued under it stop working at once.

    Parameters
    ----------
    ctx
        The Typer context.
    rule_id
        The rule's id.
    principal
        A new service account.
    claim
        New claim conditions, replacing the old ones.
    kind
        New kinds.
    start_on_deploy_jobs
        Whether its deploys start the project's on_deploy jobs.

    Raises
    ------
    CliError
        If nothing would change (exit 2).
    """
    state = state_of(ctx)
    if principal is None and not claim and not kind and start_on_deploy_jobs is None:
        raise CliError(
            "Nothing to change; pass --principal, --claim, --kind or --start-on-deploy-jobs."
        )
    rule = state.client().ci_trust_rules.update(
        rule_id,
        principal=principal,
        kinds=kind or None,
        conditions=parse_key_values(claim, "--claim") if claim else None,
        start_on_deploy_jobs=start_on_deploy_jobs,
    )
    _output.done(state, f"Updated CI trust rule {rule.id}.", rule)


@ci_trust_app.command("enable")
def enable(ctx: typer.Context, rule_id: str) -> None:
    """Enable CI trust rule RULE_ID.

    Parameters
    ----------
    ctx
        The Typer context.
    rule_id
        The rule's id.
    """
    state = state_of(ctx)
    rule = state.client().ci_trust_rules.enable(rule_id)
    _output.done(state, f"Enabled CI trust rule {rule.id}.", rule)


@ci_trust_app.command("disable")
def disable(ctx: typer.Context, rule_id: str) -> None:
    """Disable CI trust rule RULE_ID; the tokens issued under it stop working at once.

    Parameters
    ----------
    ctx
        The Typer context.
    rule_id
        The rule's id.
    """
    state = state_of(ctx)
    rule = state.client().ci_trust_rules.disable(rule_id)
    _output.done(
        state,
        f"Disabled CI trust rule {rule.id}; the tokens issued under it no longer work.",
        rule,
    )


@ci_trust_app.command("delete")
def delete(
    ctx: typer.Context,
    rule_id: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete CI trust rule RULE_ID; the tokens issued under it stop working at once.

    Parameters
    ----------
    ctx
        The Typer context.
    rule_id
        The rule's id.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(
        state,
        f"Delete CI trust rule {_output.inline(rule_id)}? CI jobs it accepts can no longer deploy.",
        yes,
    )
    state.client().ci_trust_rules.delete(rule_id)
    _output.done(state, f"Deleted CI trust rule {_output.inline(rule_id)}.")


def _read_claims(source: str) -> dict[str, Any]:
    """Read the claims of an ID token from a JSON file, or from standard input for ``-``.

    Parameters
    ----------
    source
        A path, or ``-``.

    Returns
    -------
    dict
        The claims.

    Raises
    ------
    CliError
        If the file cannot be read, is larger than 64 KiB, or is not a JSON object.
    """
    try:
        text = (
            sys.stdin.read(_MAX_CLAIMS_BYTES + 1)
            if source == "-"
            else Path(source).read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise CliError(f"Cannot read {_output.inline(source)}: {exc.strerror}.") from exc
    if len(text) > _MAX_CLAIMS_BYTES:
        raise CliError("The claims are larger than 64 KiB; paste only the ID token's claims.")
    try:
        claims = json.loads(text)
    except ValueError:
        claims = None
    if not isinstance(claims, dict):
        raise CliError(
            "The claims must be one JSON object, the decoded payload of the ID token (not the "
            'token itself), such as {"iss": "...", "repository_id": "...", "ref": "..."}.'
        )
    return claims


@ci_trust_app.command("explain")
def explain(
    ctx: typer.Context,
    claims: Annotated[
        str,
        typer.Option(
            "--claims",
            help="A JSON file with the ID token's decoded claims, or - for standard input.",
        ),
    ],
    env: Annotated[str, typer.Option("--env", help="The environment the job deploys to.")],
    repo: Annotated[str, typer.Option("--repo", help="The repository it deploys.")],
    issuer: Annotated[
        str | None, typer.Option("--issuer", help="The issuer; the claims' iss by default.")
    ] = None,
) -> None:
    """Say which rule a CI job's claims would match, and why the others do not.

    Needs no ID token: give the decoded claims, for example from the job's log.

    Parameters
    ----------
    ctx
        The Typer context.
    claims
        Where the claims are.
    env
        The environment.
    repo
        The repository.
    issuer
        The issuer.
    """
    state = state_of(ctx)
    report = state.client().ci_trust_rules.explain(
        _read_claims(claims), environment=env, repository=repo, issuer=issuer
    )
    if state.output == "json":
        _output.print_json(report)
        return
    lines = [f"Verdict: {_output.inline(report.verdict, 500)}"]
    for rule in report.rules:
        if rule.matches:
            lines.append(f"  {rule.rule_id}: matches")
            continue
        lines.append(f"  {rule.rule_id}: does not match")
        lines.extend(f"    - {_output.inline(reason, 500)}" for reason in rule.reasons)
    if not report.rules:
        lines.append("  (no rules; create one with lakana ci-trust create)")
    typer.echo(_output.safe_text("\n".join(lines)))


def register(app: typer.Typer) -> None:
    """Add the `ci-trust` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(ci_trust_app, name="ci-trust")
