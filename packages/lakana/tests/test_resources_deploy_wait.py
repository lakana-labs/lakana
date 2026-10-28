"""`Deploys.wait`: the reason a queued deploy gives and the `on_update` callback."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import Deploy
from lakana.errors import DeployFailed
from lakana.resources.deploys import Deploys, deploy_from_json

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_BODY: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000ee",
    "environment": "dev",
    "repository": "analytics",
    "kind": "deploy",
    "status": "queued",
    "requested_ref": "main",
    "resolved_sha": None,
    "rollback_to_project_version_id": None,
    "error_message": None,
    "diff": None,
    "project_version_id": None,
    "principal_id": None,
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
}


def _deploys(bodies: list[dict[str, Any]]) -> Deploys:
    """Build the resource over a transport whose answers are the given bodies, in order.

    Parameters
    ----------
    bodies
        The bodies, one per poll; the last repeats.

    Returns
    -------
    Deploys
        The resource.
    """
    polled: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies[min(len(polled), len(bodies) - 1)]
        polled.append(1)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Deploys(HttpTransport(_CONFIG, http_client=client))


def test_a_queued_deploy_carries_its_reason_and_one_without_has_none() -> None:
    waiting = {"code": "no_worker", "message": "No worker is running.", "workers": 0}
    reason = deploy_from_json({**_BODY, "waiting_for": waiting}).waiting_for
    assert reason is not None
    assert (reason.code, reason.message, reason.workers) == (
        "no_worker",
        "No worker is running.",
        0,
    )
    assert deploy_from_json(_BODY).waiting_for is None
    assert deploy_from_json({**_BODY, "waiting_for": {"code": 3}}).waiting_for is None


def test_wait_calls_on_update_with_every_polled_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lakana.resources.deploys.time.sleep", lambda seconds: None)
    deploys = _deploys(
        [
            {**_BODY, "waiting_for": {"code": "no_worker", "message": "None."}},
            {**_BODY, "waiting_for": {"code": "starting", "message": "Soon."}},
            {**_BODY, "status": "succeeded"},
        ]
    )
    seen: list[tuple[str, str | None]] = []

    def on_update(deploy: Deploy) -> None:
        """Record the status and the code.

        Parameters
        ----------
        deploy
            The polled run.
        """
        seen.append((deploy.status, deploy.waiting_for.code if deploy.waiting_for else None))

    result = deploys.wait("dev", _BODY["id"], on_update=on_update)

    assert result.status == "succeeded"
    assert seen == [("queued", "no_worker"), ("queued", "starting"), ("succeeded", None)]


def test_on_update_sees_a_failed_deploy_before_the_error() -> None:
    deploys = _deploys([{**_BODY, "status": "failed", "error_message": "boom"}])
    seen: list[str] = []
    with pytest.raises(DeployFailed, match="boom"):
        deploys.wait("dev", _BODY["id"], on_update=lambda deploy: seen.append(deploy.status))
    assert seen == ["failed"]


def test_an_exception_in_on_update_ends_the_wait() -> None:
    def on_update(deploy: Deploy) -> None:
        """Refuse every run.

        Parameters
        ----------
        deploy
            The polled run.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("display broke")

    with pytest.raises(RuntimeError, match="display broke"):
        _deploys([_BODY]).wait("dev", _BODY["id"], on_update=on_update)


_STARTS: list[dict[str, Any]] = [
    {
        "job": "refresh",
        "outcome": "started",
        "run_id": "00000000-0000-0000-0000-0000000000a1",
        "waits_for_run_id": None,
        "reason": None,
        "message": None,
    },
    {
        "job": "nightly",
        "outcome": "not_started",
        "run_id": None,
        "waits_for_run_id": None,
        "reason": "lacks_run",
        "message": "job `nightly` was not started: ci may not RUN in `dev`.",
    },
]


def test_a_deploy_reads_its_job_starts_and_an_older_server_has_none() -> None:
    deploy = deploy_from_json({**_BODY, "status": "succeeded", "job_starts": _STARTS})
    started, refused = deploy.job_starts
    assert (started.job, started.outcome, started.run_id) == (
        "refresh",
        "started",
        "00000000-0000-0000-0000-0000000000a1",
    )
    assert (refused.outcome, refused.reason, refused.message) == (
        "not_started",
        "lacks_run",
        "job `nightly` was not started: ci may not RUN in `dev`.",
    )
    assert deploy_from_json(_BODY).job_starts == ()


def test_wait_returns_once_the_job_starts_are_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """A succeeded deploy whose starts are still pending is polled again, not returned."""
    monkeypatch.setattr("lakana.resources.deploys.time.sleep", lambda seconds: None)
    pending = [{"job": "refresh", "outcome": "pending"}]
    deploys = _deploys(
        [
            {**_BODY, "status": "succeeded", "job_starts": pending},
            {**_BODY, "status": "succeeded", "job_starts": _STARTS},
        ]
    )
    seen: list[tuple[str, ...]] = []

    result = deploys.wait(
        "dev",
        _BODY["id"],
        on_update=lambda deploy: seen.append(tuple(s.outcome for s in deploy.job_starts)),
    )

    assert [s.outcome for s in result.job_starts] == ["started", "not_started"]
    assert seen == [("pending",), ("started", "not_started")]


def test_wait_gives_up_on_pending_starts_at_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lakana.resources.deploys.time.sleep", lambda seconds: None)
    pending = [{"job": "refresh", "outcome": "pending"}]
    deploys = _deploys([{**_BODY, "status": "succeeded", "job_starts": pending}])
    with pytest.raises(TimeoutError, match="its job starts are not recorded yet"):
        deploys.wait("dev", _BODY["id"], timeout=0)
