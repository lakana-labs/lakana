"""The messages a webhook receives: json, slack and teams, signing, and the facts they carry."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    JobVersion,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    RunTrigger,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.notifications.payload import Facts, build, facts_for_run, sample_facts, sign

GOLDEN = Path(__file__).parent / "notification_golden"
FINISHED = datetime(2026, 10, 14, 6, 5, tzinfo=UTC)
RUN_ID = "11111111-2222-3333-4444-555555555555"
DELIVERY = "99999999-8888-7777-6666-555555555555"
FORMATS = ("json", "slack", "teams")


def _facts(**changes: object) -> Facts:
    """Build facts for a failed run, with changes.

    Parameters
    ----------
    **changes
        Fields to override.

    Returns
    -------
    Facts
        The facts.
    """
    fields: dict = {
        "job": "daily",
        "environment": "prod",
        "repository": "analytics",
        "commit": "a" * 40,
        "run_id": RUN_ID,
        "trigger": "schedule",
        "finished_at": FINISHED,
        "status": "failed",
        "failed_tasks": ("silver.clean",),
        "failed_checks": ("silver.clean/no_nulls",),
        "delivery_id": DELIVERY,
    }
    fields.update(changes)
    return Facts(**fields)


CASES = {
    "failed_check": ("failed", _facts()),
    "failed": ("failed", _facts(failed_checks=())),
    "recovered": (
        "recovered",
        _facts(status="succeeded", failed_tasks=(), failed_checks=()),
    ),
    "test": ("test", sample_facts(DELIVERY)),
}


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("fmt", FORMATS)
def test_payload_matches_golden_file(fmt: str, case: str) -> None:
    event, facts = CASES[case]
    body, headers = build(fmt, event, facts)
    assert headers == {"Content-Type": "application/json"}
    assert body == (GOLDEN / f"{fmt}_{case}.json").read_bytes()


def test_json_payload_is_versioned_and_carries_the_delivery_id() -> None:
    body, _ = build("json", "failed", _facts())
    data = json.loads(body)
    assert data["version"] == 1
    assert data["delivery_id"] == DELIVERY
    assert data["event"] == "failed"
    assert data["command"] == f"lakana runs show {RUN_ID} --env prod"


def test_unknown_event_is_refused() -> None:
    with pytest.raises(ValueError, match="event"):
        build("json", "exploded", _facts())


def test_unknown_format_is_refused() -> None:
    with pytest.raises(ValueError, match="format"):
        build("xml", "failed", _facts())


def test_signature_matches_a_reference_hmac() -> None:
    body = b'{"a":1}'
    headers = sign(body, "s3cret", 1760421900)
    expected = hmac.new(b"s3cret", b"1760421900." + body, hashlib.sha256).hexdigest()
    assert headers == {
        "X-Lakana-Signature": f"sha256={expected}",
        "X-Lakana-Timestamp": "1760421900",
    }


def test_signature_depends_on_the_timestamp() -> None:
    assert sign(b"x", "k", 1) != sign(b"x", "k", 2)


def test_names_with_special_characters_are_escaped() -> None:
    nasty = _facts(
        job="a<b>&*c*",
        failed_tasks=("t`x`", "<!channel>"),
        failed_checks=("[l](http://evil)/_c_",),
    )
    slack = json.loads(build("slack", "failed", nasty)[0])["text"]
    assert "<!channel>" not in slack
    assert "&lt;!channel&gt;" in slack
    assert "a&lt;b&gt;&amp;" in slack
    teams = build("teams", "failed", nasty)[0].decode()
    assert "[l](http" not in teams
    assert "\\\\[l\\\\]" in teams
    assert "\\\\<!channel\\\\>" in teams
    assert "`t'x'`" in slack
    assert "t`x`" not in slack
    assert json.loads(build("json", "failed", nasty)[0])["job"] == "a<b>&*c*"


def test_slack_title_puts_names_in_code_spans() -> None:
    text = json.loads(build("slack", "failed", _facts(job="*x*", environment="@channel"))[0])[
        "text"
    ]
    assert text.splitlines()[0] == "Lakana: job `*x*` failed in `@channel`"


def test_slack_shows_unknown_for_an_empty_value() -> None:
    facts = _facts(trigger="", finished_at=None)
    text = json.loads(build("slack", "failed", facts)[0])["text"]
    assert "``" not in text
    assert "trigger `unknown`" in text
    assert "finished `unknown`" in text


def _seed(
    session: Session,
    outcomes: list[tuple[str, str, CheckOutcome]],
    *,
    generated: tuple[str, ...] = (),
) -> Run:
    """Seed a failed run whose errors carry a marker.

    Parameters
    ----------
    session
        Database session.
    outcomes
        (check name, on_failure, outcome) per check, all on one failed task.
    generated
        The names of the checks generated from the asset's header.

    Returns
    -------
    Run
        The run.
    """
    env = Environment(id=uuid.uuid4(), name="prod", storage_root="file:///tmp/p")
    repo = Repository(
        id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
    )
    session.add_all([env, repo])
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env.id,
        commit_sha="b" * 40,
        manifest_json={},
        code_uri="file:///tmp/p/code.tar.gz",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=repo.id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        selection_json={},
        status=RunStatus.FAILED,
        error_message="SECRET-MARKER run",
        finished_at=FINISHED,
        job="daily",
        trigger=RunTrigger.MANUAL,
    )
    session.add(run)
    session.flush()
    tasks = {}
    for asset, status in (("silver.clean", TaskStatus.FAILED), ("silver.ok", TaskStatus.SUCCEEDED)):
        tasks[asset] = TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset=asset,
            status=status,
            max_attempts=1,
            timeout_seconds=60,
            retry_delay_seconds=0,
            error_message="SECRET-MARKER task" if status is TaskStatus.FAILED else None,
        )
        session.add(tasks[asset])
    session.flush()
    session.add(
        TaskAttempt(
            id=uuid.uuid4(),
            task_run_id=tasks["silver.clean"].id,
            attempt=1,
            worker="w",
            started_at=FINISHED,
            error_message="SECRET-MARKER attempt",
        )
    )
    session.flush()
    for name, on_failure, outcome in outcomes:
        session.add(
            CheckResult(
                id=uuid.uuid4(),
                run_id=run.id,
                task_run_id=tasks["silver.clean"].id,
                environment_id=env.id,
                check_name=name,
                check_path=f"checks/{name}.sql",
                table_name="silver.clean",
                on_failure=on_failure,
                outcome=outcome,
                error="SECRET-MARKER check" if outcome is CheckOutcome.ERRORED else None,
                failing_rows=7 if outcome is CheckOutcome.FAILED else None,
                started_at=FINISHED,
                finished_at=FINISHED,
                generated=name in generated,
            )
        )
    session.flush()
    return run


def test_facts_for_run_reads_names_and_no_error_text(session: Session) -> None:
    run = _seed(
        session,
        [
            ("blocks", "block", CheckOutcome.FAILED),
            ("keeps", "keep", CheckOutcome.ERRORED),
            ("warns", "warn", CheckOutcome.FAILED),
            ("fine", "block", CheckOutcome.PASSED),
        ],
    )
    facts = facts_for_run(session, run, DELIVERY)
    assert facts.job == "daily"
    assert facts.environment == "prod"
    assert facts.repository == "analytics"
    assert facts.commit == "b" * 40
    assert facts.trigger == "manual"
    assert facts.status == "failed"
    assert facts.finished_at == FINISHED
    assert facts.failed_tasks == ("silver.clean",)
    assert facts.failed_checks == ("silver.clean/blocks", "silver.clean/keeps")
    for fmt in FORMATS:
        for event in ("failed", "recovered", "test"):
            assert b"SECRET-MARKER" not in build(fmt, event, facts)[0]


def test_a_failed_reference_check_is_said_but_never_named(session: Session) -> None:
    """The endpoint is no reader whose rights can be asked: no reference check name or count."""
    run = _seed(
        session,
        [
            ("blocks", "block", CheckOutcome.FAILED),
            ("ref_store_id", "block", CheckOutcome.FAILED),
            ("ref_handwritten", "block", CheckOutcome.FAILED),
        ],
        generated=("ref_store_id",),
    )
    facts = facts_for_run(session, run, DELIVERY)
    assert facts.failed_checks == ("silver.clean/blocks", "silver.clean/ref_handwritten")
    assert facts.reference_check_failed
    data = json.loads(build("json", "failed", facts)[0])
    assert data["reference_check_failed"] is True
    slack = json.loads(build("slack", "failed", facts)[0])["text"]
    note = f"A reference check failed; see `lakana runs show {facts.run_id} --env prod`"
    assert note in slack
    teams = build("teams", "failed", facts)[0].decode()
    assert "A reference check failed" in teams
    for fmt in FORMATS:
        body = build(fmt, "failed", facts)[0].decode()
        assert "ref_store_id" not in body
        assert "7" not in body.replace(facts.run_id, "").replace(DELIVERY, "")


def test_without_a_failed_reference_check_the_json_has_no_flag() -> None:
    assert "reference_check_failed" not in json.loads(build("json", "failed", _facts())[0])
