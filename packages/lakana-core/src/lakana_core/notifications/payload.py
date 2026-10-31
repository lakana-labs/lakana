"""The messages a webhook receives, in `json`, `slack` and `teams`, and their signature.

The builders are pure: facts in, bytes out. `facts_for_run` is the one function here that reads the
database. A message carries names and metadata only: never table data, secrets, or the error text of
a task, an attempt or a check, because those can quote row values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.identifiers import NOTIFY_FORMATS
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    JobVersion,
    Repository,
    Run,
    TaskRun,
    TaskStatus,
)
from lakana_core.notifications.events import columns_changed_tables
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.hidden_checks import is_reference_check

_CHANGED = "The run removed or retyped columns of"
_UNFINISHED = "Checks never finished:"
_UNFINISHED_NEXT = (
    "Lakana stopped trying them again; {command} says whether each table was set back and what "
    "to do next"
)

PAYLOAD_VERSION = 1
"""The `version` of the `json` payload; raised only by a change that breaks a receiver."""

FORMATS = NOTIFY_FORMATS
"""The formats a webhook can ask for."""

_TITLES = {
    "failed": "Lakana: job {job} failed in {environment}",
    "recovered": "Lakana: job {job} succeeded again in {environment}",
    "columns_changed": "Lakana: job {job} changed table columns in {environment}",
    "test": "Lakana: test message for job {job} in {environment}",
}


@dataclass(frozen=True, slots=True)
class Facts:
    """What a message says about a run.

    Parameters
    ----------
    job
        The job's name.
    environment
        The environment's name.
    repository
        The repository's name.
    commit
        The commit of the project version the run ran under.
    run_id
        The run's id.
    trigger
        What started the run (`schedule`, `api` or `manual`), or an empty string.
    finished_at
        When the run finished; `None` if unknown.
    status
        The run's final status.
    failed_tasks
        Names of the tasks that failed.
    failed_checks
        Names (`table/check`) of the blocking checks that failed or errored, generated reference
        checks left out.
    delivery_id
        The delivery's id, for a receiver to deduplicate on.
    reference_check_failed
        Whether a generated reference check failed or errored. Such a check is never named: its
        outcome tells of the table it points at, and the endpoint is no reader whose right to
        read that table can be asked. `lakana runs show` shows it to a reader who may.
    columns_changed
        The run's own tables whose columns a write removed or replaced, whether or not a row
        filter or column mask uses them. Nothing more is said: the endpoint is no administrator,
        and only an administrator may learn that a policy exists (`lakana runs show` shows them).
    checks_unfinished
        The failed tasks whose checks never finished (`FailureCode.CHECKS_UNFINISHED`): the
        platform stopped trying them and set their tables back where it could.
    """

    job: str
    environment: str
    repository: str
    commit: str
    run_id: str
    trigger: str
    finished_at: datetime | None
    status: str
    failed_tasks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    delivery_id: str
    reference_check_failed: bool = False
    columns_changed: tuple[str, ...] = ()
    checks_unfinished: tuple[str, ...] = ()

    @property
    def command(self) -> str:
        """The command that shows the run.

        Returns
        -------
        str
            ``lakana runs show RUN --env ENV``.
        """
        return f"lakana runs show {self.run_id} --env {self.environment}"

    @property
    def finished(self) -> str:
        """The finish time in UTC, ISO 8601.

        Returns
        -------
        str
            The time, or an empty string when unknown.
        """
        if self.finished_at is None:
            return ""
        return self.finished_at.astimezone(UTC).isoformat()


def facts_for_run(session: Session, run: Run, delivery_id: UUID | str) -> Facts:
    """Read the facts of a finished run.

    Reads names and metadata only; it never reads an error text of a task, attempt or check.

    Parameters
    ----------
    session
        Database session.
    run
        The finished run.
    delivery_id
        The delivery's id.

    Returns
    -------
    Facts
        The facts.
    """
    environment = session.get(Environment, run.environment_id)
    repository = session.get(Repository, run.repository_id)
    version = session.get(JobVersion, run.job_version_id)
    failed = session.execute(
        select(TaskRun.asset, TaskRun.failure_code)
        .where(TaskRun.run_id == run.id, TaskRun.status == TaskStatus.FAILED)
        .order_by(TaskRun.asset)
    ).all()
    tasks = [asset for asset, _ in failed]
    checks = session.execute(
        select(CheckResult.table_name, CheckResult.check_name, CheckResult.generated)
        .where(
            CheckResult.run_id == run.id,
            CheckResult.outcome.in_((CheckOutcome.FAILED, CheckOutcome.ERRORED)),
            CheckResult.on_failure.in_(("block", "keep")),
        )
        .order_by(CheckResult.table_name, CheckResult.check_name)
    ).all()
    return Facts(
        job=run.job or "",
        environment=environment.name if environment is not None else "",
        repository=repository.name if repository is not None else "",
        commit=version.commit_sha if version is not None else "",
        run_id=str(run.id),
        trigger=run.trigger.value if run.trigger is not None else "",
        finished_at=run.finished_at,
        status=run.status.value,
        failed_tasks=tuple(tasks),
        failed_checks=tuple(
            f"{table}/{name}"
            for table, name, generated in checks
            if not is_reference_check(name, generated)
        ),
        delivery_id=str(delivery_id),
        reference_check_failed=any(
            is_reference_check(name, generated) for _, name, generated in checks
        ),
        columns_changed=columns_changed_tables(session, run.id),
        checks_unfinished=tuple(
            asset for asset, code in failed if code == FailureCode.CHECKS_UNFINISHED.value
        ),
    )


def sample_facts(delivery_id: UUID | str) -> Facts:
    """Build clearly fake facts, for `notify-test`.

    Parameters
    ----------
    delivery_id
        The delivery's id.

    Returns
    -------
    Facts
        Facts of an invented run of the job ``notify-test``.
    """
    return Facts(
        job="notify-test",
        environment="test",
        repository="notify-test",
        commit="0" * 40,
        run_id="00000000-0000-0000-0000-000000000000",
        trigger="manual",
        finished_at=datetime(2000, 1, 1, tzinfo=UTC),
        status="succeeded",
        failed_tasks=(),
        failed_checks=(),
        delivery_id=str(delivery_id),
    )


def sign(body: bytes, key: str | bytes, timestamp: int) -> dict[str, str]:
    """Sign a body for the `json` format.

    The HMAC-SHA256 covers ``"{timestamp}.{body}"``, so a captured message cannot be replayed with
    a fresh timestamp; a receiver rejects a timestamp that is too old.

    Parameters
    ----------
    body
        The request body.
    key
        The signing key.
    timestamp
        Unix seconds, sent in `X-Lakana-Timestamp`.

    Returns
    -------
    dict
        The headers `X-Lakana-Signature` (``sha256=<hex>``) and `X-Lakana-Timestamp`.
    """
    raw_key = key.encode() if isinstance(key, str) else key
    digest = hmac.new(raw_key, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Lakana-Signature": f"sha256={digest}", "X-Lakana-Timestamp": str(timestamp)}


def build(fmt: str, event: str, facts: Facts) -> tuple[bytes, dict[str, str]]:
    """Build the request body and headers of a message.

    Parameters
    ----------
    fmt
        `json`, `slack` or `teams`.
    event
        `failed`, `recovered`, `columns_changed` or `test`.
    facts
        What the message says.

    Returns
    -------
    tuple
        The body (UTF-8 JSON) and the headers.

    Raises
    ------
    ValueError
        If the format or the event is unknown.
    """
    if event not in _TITLES:
        raise ValueError(f"unknown notification event {event!r}")
    if fmt == "json":
        payload = _json(event, facts)
    elif fmt == "slack":
        payload = {"text": _slack(event, facts)}
    elif fmt == "teams":
        payload = _teams(event, facts)
    else:
        raise ValueError(f"unknown notification format {fmt!r}; use one of {', '.join(FORMATS)}")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return body.encode(), {"Content-Type": "application/json"}


def _json(event: str, facts: Facts) -> dict[str, Any]:
    """Build the `json` payload.

    Parameters
    ----------
    event
        The event.
    facts
        The facts.

    Returns
    -------
    dict
        The payload.
    """
    return {
        "version": PAYLOAD_VERSION,
        "event": event,
        "delivery_id": facts.delivery_id,
        "job": facts.job,
        "environment": facts.environment,
        "repository": facts.repository,
        "commit": facts.commit,
        "run_id": facts.run_id,
        "trigger": facts.trigger,
        "finished_at": facts.finished,
        "status": facts.status,
        "failed_tasks": list(facts.failed_tasks),
        "failed_checks": list(facts.failed_checks),
        "command": facts.command,
    } | (
        ({"reference_check_failed": True} if facts.reference_check_failed else {})
        | ({"columns_changed": list(facts.columns_changed)} if facts.columns_changed else {})
        | ({"checks_unfinished": list(facts.checks_unfinished)} if facts.checks_unfinished else {})
    )


def _title(event: str, facts: Facts) -> str:
    """Name the event in one line (unescaped).

    Parameters
    ----------
    event
        The event.
    facts
        The facts.

    Returns
    -------
    str
        The title.
    """
    return _TITLES[event].format(job=facts.job, environment=facts.environment)


def _slack_escape(text: str) -> str:
    """Escape text for Slack mrkdwn, which also reads `<!channel>` and `<url|label>`.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    str
        The text with ``&``, ``<`` and ``>`` replaced, and backticks turned into apostrophes so the
        text cannot end a code span.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


def _code(value: str) -> str:
    """Wrap a value in a Slack code span, so mrkdwn inside it does not format the message.

    Parameters
    ----------
    value
        The value; an empty one stands for something unknown.

    Returns
    -------
    str
        The escaped value in backticks, or `unknown` when the value is empty.
    """
    return f"`{_slack_escape(value or 'unknown')}`"


def _slack(event: str, facts: Facts) -> str:
    """Build the text of the `slack` message; other chat tools read it alike.

    Every name sits in a code span, so mrkdwn in a name does not format the message and an
    `@channel` in a name pings nobody.

    Parameters
    ----------
    event
        The event.
    facts
        The facts.

    Returns
    -------
    str
        The text.
    """
    title = _TITLES[event].format(job=_code(facts.job), environment=_code(facts.environment))
    lines = [title]
    lines.append(f"Repository {_code(facts.repository)}, commit {_code(facts.commit[:12])}")
    lines.append(
        f"Run {_code(facts.run_id)}, trigger {_code(facts.trigger)}, "
        f"status {_code(facts.status)}, finished {_code(facts.finished)}"
    )
    if facts.failed_tasks:
        lines.append("Failed tasks: " + ", ".join(_code(t) for t in facts.failed_tasks))
    if facts.failed_checks:
        lines.append("Failed checks: " + ", ".join(_code(c) for c in facts.failed_checks))
    if facts.reference_check_failed:
        lines.append(f"A reference check failed; see {_code(facts.command)}")
    if facts.columns_changed:
        tables = ", ".join(_code(t) for t in facts.columns_changed)
        lines.append(
            f"{_CHANGED} {tables}; an administrator can see in {_code(facts.command)} what to check"
        )
    if facts.checks_unfinished:
        tables = ", ".join(_code(t) for t in facts.checks_unfinished)
        next_step = _UNFINISHED_NEXT.format(command=_code(facts.command))
        lines.append(f"{_UNFINISHED} {tables}. {next_step}")
    lines.append(f"Details: {_code(facts.command)}")
    lines.append(f"Delivery {_code(facts.delivery_id)}")
    return "\n".join(lines)


_MARKDOWN = re.compile(r"([\\`*_\[\]<>~|#&])")


def _markdown_escape(text: str) -> str:
    """Escape the Markdown characters of an Adaptive Card text.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    str
        The text with a backslash before each special character.
    """
    return _MARKDOWN.sub(r"\\\1", text)


def _teams(event: str, facts: Facts) -> dict[str, Any]:
    """Build the payload of a Teams webhook created with Workflows.

    The shape follows Microsoft's "Create an Incoming Webhook" page, section "Create Webhooks using
    Workflows" and its example: a message whose `attachments` hold one Adaptive Card
    (https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook,
    checked 2026-10-14). The card is version 1.4: a TextBlock title and a FactSet.

    Parameters
    ----------
    event
        The event.
    facts
        The facts.

    Returns
    -------
    dict
        The payload.
    """
    pairs = [
        ("Job", facts.job),
        ("Environment", facts.environment),
        ("Repository", facts.repository),
        ("Commit", facts.commit),
        ("Run", facts.run_id),
        ("Trigger", facts.trigger),
        ("Status", facts.status),
        ("Finished", facts.finished),
    ]
    if facts.failed_tasks:
        pairs.append(("Failed tasks", ", ".join(facts.failed_tasks)))
    if facts.failed_checks:
        pairs.append(("Failed checks", ", ".join(facts.failed_checks)))
    if facts.reference_check_failed:
        pairs.append(("Reference check", f"A reference check failed; see {facts.command}"))
    if facts.columns_changed:
        tables = ", ".join(facts.columns_changed)
        pairs.append(
            (
                "Columns changed",
                f"{_CHANGED} {tables}; an administrator can see in {facts.command} what to check",
            )
        )
    if facts.checks_unfinished:
        tables = ", ".join(facts.checks_unfinished)
        next_step = _UNFINISHED_NEXT.format(command=facts.command)
        pairs.append(("Checks never finished", f"{tables}. {next_step}"))
    pairs += [("Details", facts.command), ("Delivery", facts.delivery_id)]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": _markdown_escape(_title(event, facts)),
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [{"title": k, "value": _markdown_escape(v)} for k, v in pairs],
            },
        ],
    }
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }
