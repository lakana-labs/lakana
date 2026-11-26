"""Deploy resources: `Client.deploys`, `.deployments`, `.project_versions`, `.orphans`."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any, NamedTuple

from lakana._http import (
    NEXT_CURSOR_HEADER,
    HttpTransport,
    every_page,
    query_params,
    route_of_a_newer_server,
)
from lakana._models import (
    Deploy,
    DeployAccepted,
    DeployJobStart,
    Deployment,
    DeployPage,
    Orphan,
    Orphans,
    ProjectVersion,
)
from lakana._routes import ROUTES
from lakana.errors import DeployFailed, JobStartsNotRecorded
from lakana.resources.queries import waiting_for_from_json
from lakana.resources.tables import actions_from_json

_LIST_ALL_INSTEAD = "list one environment with `lakana deploys list --env ENV`"
"""What to do instead of `Deploys.list_all` against a server without `GET /api/v1/deploys`."""


def _when(value: str | None) -> datetime | None:
    """Parse an optional ISO timestamp.

    Parameters
    ----------
    value
        The timestamp or `None`.

    Returns
    -------
    datetime or None
        The parsed value.
    """
    return datetime.fromisoformat(value) if value is not None else None


def job_start_from_json(item: dict[str, Any]) -> DeployJobStart:
    """Build a `DeployJobStart` from a `DeployJobStartOut` body.

    Parameters
    ----------
    item
        The decoded entry.

    Returns
    -------
    DeployJobStart
        The entry.
    """
    return DeployJobStart(
        job=item["job"],
        outcome=item["outcome"],
        run_id=item.get("run_id"),
        waits_for_run_id=item.get("waits_for_run_id"),
        reason=item.get("reason"),
        message=item.get("message"),
    )


def deploy_from_json(data: dict[str, Any]) -> Deploy:
    """Build a `Deploy` from a `DeployOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Deploy
        The run.
    """
    return Deploy(
        id=data["id"],
        environment=data["environment"],
        repository=data["repository"],
        kind=data["kind"],
        status=data["status"],
        requested_ref=data["requested_ref"],
        resolved_sha=data["resolved_sha"],
        rollback_to_project_version_id=data["rollback_to_project_version_id"],
        error_message=data["error_message"],
        diff=data["diff"],
        project_version_id=data["project_version_id"],
        principal_id=data["principal_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
        started_at=_when(data["started_at"]),
        finished_at=_when(data["finished_at"]),
        hidden_adopted=data.get("hidden_adopted", 0),
        checks=data.get("checks"),
        warnings=tuple(data.get("warnings", ())),
        waiting_for=waiting_for_from_json(data.get("waiting_for")),
        job_starts=tuple(job_start_from_json(item) for item in data.get("job_starts", ())),
        actions=tuple(data.get("actions", [])),
        submitted_by=data.get("submitted_by"),
        jobs=None if data.get("jobs") is None else tuple(data["jobs"]),
        commit_subject=data.get("commit_subject"),
    )


def deployment_from_json(item: dict[str, Any]) -> Deployment:
    """Build a `Deployment` from a `DeploymentOut` body.

    Parameters
    ----------
    item
        The decoded body.

    Returns
    -------
    Deployment
        The deployment.
    """
    return Deployment(
        repository=item["repository"],
        project_version_id=item["project_version_id"],
        commit_sha=item["commit_sha"],
        updated_at=datetime.fromisoformat(item["updated_at"]),
        run_as=item.get("run_as"),
    )


_DeployList = list[Deploy]
_DeploymentList = list[Deployment]


class Deploys:
    """Queue and follow deploys, dry runs and rollbacks.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        environment: str,
        repository: str,
        *,
        kind: str = "deploy",
        ref: str | None = None,
        to_sha: str | None = None,
        to_previous: bool = False,
    ) -> DeployAccepted:
        """Queue a deploy (needs `DEPLOY` on the environment).

        Parameters
        ----------
        environment
            Target environment.
        repository
            Registered repository name.
        kind
            ``deploy``, ``dry_run`` or ``rollback``.
        ref
            Deploy/dry run: ref or full SHA.
        to_sha
            Rollback: an already-deployed full SHA.
        to_previous
            Rollback: the project version current before the present one.

        Returns
        -------
        DeployAccepted
            The queued run's id.

        Raises
        ------
        lakana.errors.PermissionDenied
            Without `DEPLOY`.
        lakana.errors.ValidationError
            For a malformed request.
        """
        response = self._transport.request(
            ROUTES["deploys.create"],
            path_params={"env": environment},
            json={
                "repository": repository,
                "kind": kind,
                "ref": ref,
                "to_sha": to_sha,
                "to_previous": to_previous,
            },
        )
        data = response.json()
        return DeployAccepted(id=data["id"], status=data["status"])

    def get(self, environment: str, deploy_id: str) -> Deploy:
        """Show one deploy run.

        Parameters
        ----------
        environment
            Environment name.
        deploy_id
            The run's id.

        Returns
        -------
        Deploy
            The run.
        """
        response = self._transport.request(
            ROUTES["deploys.get"], path_params={"env": environment, "deploy_id": deploy_id}
        )
        return deploy_from_json(response.json())

    def list(
        self,
        environment: str,
        *,
        repository: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> _DeployList:
        """List an environment's deploy history, newest first.

        The deploys of one page; `list_page` also returns the cursor of the next one.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's deploys.
        limit
            Most deploys returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `repository` with every page.
        sort
            The sort, as `list_page` takes it.
        order
            ``asc`` or ``desc``, as `list_page` takes it.

        Returns
        -------
        list of Deploy
            Newest first.

        Raises
        ------
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200 or `cursor` is not one the server issued for the
            deploys list.
        """
        return self.list_page(
            environment,
            repository=repository,
            limit=limit,
            cursor=cursor,
            sort=sort,
            order=order,
        ).items

    def list_page(
        self,
        environment: str,
        *,
        repository: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> DeployPage:
        """List one page of an environment's deploy history, with the cursor of the next page.

        Parameters
        ----------
        environment
            Environment name.
        repository
            Only this repository's deploys.
        limit
            Most deploys returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `repository` with every page.
        sort
            What the deploys are sorted by: ``created`` (the default) or ``status``, whose
            deploys of one state come newest first. Send the same sort with every page.
        order
            ``asc`` or ``desc``; by default ``desc`` for ``created``, ``asc`` for ``status``.

        Returns
        -------
        DeployPage
            The deploys, newest first, and the cursor for the next page (`None` on the last).

        Raises
        ------
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200 or `cursor` is not one the server issued for the
            deploys list.
        """
        response = self._transport.request(
            ROUTES["deploys.list"],
            path_params={"env": environment},
            params=query_params(
                repository=repository, limit=limit, cursor=cursor, sort=sort, order=order
            ),
        )
        return DeployPage(
            items=[deploy_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def list_all(
        self,
        *,
        environment: str | None = None,
        repository: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> DeployPage:
        """List one page of the deploys of every environment you may read.

        The environments are those where you hold `READ_ENVIRONMENT`; one where you do not is
        left out, so this is never refused for a missing right. The order is that of `list`,
        over all those environments, and each deploy names its `environment`; `jobs` is `None`
        (`get` has it). With `environment`, this is `list_page` of that environment.

        Parameters
        ----------
        environment
            Only this environment; then a missing right or environment is refused as by `list`.
        repository
            Only this repository's deploys.
        limit
            Most deploys returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor` of this list. Send the same `repository` with every
            page.
        sort
            The sort, as `list_page` takes it.
        order
            ``asc`` or ``desc``, as `list_page` takes it.

        Returns
        -------
        DeployPage
            The deploys, newest first, and the cursor for the next page (`None` on the last).

        Raises
        ------
        lakana.errors.PermissionDenied
            With `environment`, if you may not read it.
        lakana.errors.NotFound
            If `environment` or `repository` does not exist; or, with a sentence saying so, if
            the server is older than this package and has no `GET /api/v1/deploys`.
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200 or `cursor` is not one the server issued for this
            list.
        """
        with route_of_a_newer_server("GET /api/v1/deploys", _LIST_ALL_INSTEAD):
            response = self._transport.request(
                ROUTES["deploys.list_all"],
                params=query_params(
                    environment=environment,
                    repository=repository,
                    limit=limit,
                    cursor=cursor,
                    sort=sort,
                    order=order,
                ),
            )
        body = response.json()
        return DeployPage(
            items=[deploy_from_json(item) for item in body["items"]],
            next_cursor=body["next_cursor"],
        )

    def iter_all(
        self,
        *,
        environment: str | None = None,
        repository: str | None = None,
        sort: str | None = None,
        order: str | None = None,
        page_size: int = 100,
    ) -> Iterator[Deploy]:
        """Yield every deploy of every environment you may read, fetching pages as needed.

        Parameters
        ----------
        environment
            Only this environment.
        repository
            Only this repository's deploys.
        sort
            The sort, as `list_page` takes it.
        order
            ``asc`` or ``desc``, as `list_page` takes it.
        page_size
            Deploys per request (1 to 200).

        Yields
        ------
        Deploy
            Each deploy, once, in the order of `list_all`.

        Raises
        ------
        lakana.errors.NotFound
            As `list_all`.
        """
        yield from every_page(
            lambda cursor: self.list_all(
                environment=environment,
                repository=repository,
                limit=page_size,
                cursor=cursor,
                sort=sort,
                order=order,
            )
        )

    def wait(
        self,
        environment: str,
        deploy_id: str,
        timeout: float | None = None,
        poll_interval: float = 0.25,
        max_interval: float = 2.0,
        on_update: Callable[[Deploy], None] | None = None,
    ) -> Deploy:
        """Poll a run until it succeeds or fails, and until the jobs it starts are started.

        A deploy that starts jobs (`on_deploy: true`) is returned only once each start is
        recorded (no `job_starts` entry is ``pending``), so the result says what became of
        every job.

        Parameters
        ----------
        environment
            Environment name.
        deploy_id
            The run's id.
        timeout
            Seconds to keep polling, or `None` for no limit.
        poll_interval
            First interval, doubling up to `max_interval`.
        max_interval
            Largest interval.
        on_update
            Called with every polled `Deploy`, including the last one, before `wait` returns or
            raises (so a progress display can show why a deploy is `queued`:
            `deploy.waiting_for`). It is called on every poll, not only when something
            changed. An exception it raises ends the wait and propagates. `None` (the default)
            calls nothing.

        Returns
        -------
        Deploy
            The run, once `succeeded` and its job starts are recorded.

        Raises
        ------
        lakana.errors.DeployFailed
            If it ends `failed`.
        lakana.errors.JobStartsNotRecorded
            If `timeout` elapses after the deploy succeeded, while a job start is still
            ``pending``; a `TimeoutError` that carries the deploy.
        TimeoutError
            If `timeout` elapses before the deploy ends.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        interval = poll_interval
        while True:
            deploy = self.get(environment, deploy_id)
            if on_update is not None:
                on_update(deploy)
            starting = any(start.outcome == "pending" for start in deploy.job_starts)
            if deploy.status == "succeeded" and not starting:
                return deploy
            if deploy.status == "failed":
                raise DeployFailed(deploy.error_message or "deploy failed", deploy)
            if deadline is not None and time.monotonic() >= deadline:
                if deploy.status == "succeeded":
                    raise JobStartsNotRecorded(
                        f"Deploy {deploy_id} succeeded, but its job starts are not recorded yet "
                        f"after {timeout}s",
                        deploy,
                    )
                raise TimeoutError(f"Deploy {deploy_id} still {deploy.status} after {timeout}s")
            time.sleep(interval)
            interval = min(interval * 2, max_interval)


class Deployments:
    """Current project versions per repository.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self, environment: str) -> _DeploymentList:
        """List the current project version per repository.

        Parameters
        ----------
        environment
            Environment name.

        Returns
        -------
        list of Deployment
            Sorted by repository.
        """
        response = self._transport.request(
            ROUTES["deployments.list"], path_params={"env": environment}
        )
        return [deployment_from_json(item) for item in response.json()]

    def set_run_as(self, environment: str, repository: str, principal: str) -> Deployment:
        """Bind the service account runs write as (needs `MANAGE_ENVIRONMENT`).

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        principal
            An active service account's name.

        Returns
        -------
        Deployment
            The deployment with its binding.
        """
        response = self._transport.request(
            ROUTES["deployments.set_run_as"],
            path_params={"env": environment, "repo": repository},
            json={"principal": principal},
        )
        return deployment_from_json(response.json())

    def clear_run_as(self, environment: str, repository: str) -> Deployment:
        """Remove the run principal binding (needs `MANAGE_ENVIRONMENT`).

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.

        Returns
        -------
        Deployment
            The deployment without a binding.
        """
        response = self._transport.request(
            ROUTES["deployments.clear_run_as"],
            path_params={"env": environment, "repo": repository},
        )
        return deployment_from_json(response.json())

    def undeploy(
        self,
        environment: str,
        repository: str,
        *,
        expected_project_version_id: str | None = None,
    ) -> None:
        """Remove a repository's deployment; data stays (needs `DEPLOY`).

        Parameters
        ----------
        environment
            Environment name.
        repository
            Repository name.
        expected_project_version_id
            The project version you saw as current (`Deployment.project_version_id`); the
            undeploy is then refused if the deployment points at another one. `None` undeploys
            whatever is current.

        Raises
        ------
        lakana.errors.Conflict
            While a run, or a deploy or rollback, of the repository is queued or running there,
            or when the deployment is not at `expected_project_version_id`.
        """
        self._transport.request(
            ROUTES["deployments.undeploy"],
            path_params={"env": environment, "repo": repository},
            params=(
                None
                if expected_project_version_id is None
                else {"expected_project_version_id": expected_project_version_id}
            ),
        )


class ProjectVersions:
    """Immutable project versions and their manifests.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def get(self, environment: str, project_version_id: str) -> ProjectVersion:
        """Show a project version.

        Parameters
        ----------
        environment
            Environment name.
        project_version_id
            The project version.

        Returns
        -------
        project version
            With its caller-filtered manifest.
        """
        response = self._transport.request(
            ROUTES["project_versions.get"],
            path_params={"env": environment, "project_version_id": project_version_id},
        )
        data = response.json()
        return ProjectVersion(
            id=data["id"],
            environment=data["environment"],
            repository=data["repository"],
            commit_sha=data["commit_sha"],
            code_uri=data["code_uri"],
            code_sha256=data["code_sha256"],
            deployed_by=data["deployed_by"],
            created_at=datetime.fromisoformat(data["created_at"]),
            manifest=data["manifest"],
        )


class OrphanTables:
    """Tables earlier project versions produced that current ones do not.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self, environment: str, *, limit: int | None = None, cursor: str | None = None
    ) -> Orphans:
        """List one page of the orphans visible to the caller.

        An orphan that was dropped and can still be restored is listed with ``exists=False``.
        `iter` walks every page.

        Parameters
        ----------
        environment
            Environment name.
        limit
            Most orphans returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`.

        Returns
        -------
        Orphans
            Readable orphans, a hidden count and the cursor of the next page.
        """
        response = self._transport.request(
            ROUTES["orphans.list"],
            path_params={"env": environment},
            params=query_params(limit=limit, cursor=cursor),
        )
        data = response.json()
        return Orphans(
            orphans=[
                Orphan(
                    repository=o["repository"],
                    table=o["table"],
                    kind=o.get("kind"),
                    exists=o.get("exists", True),
                    actions=actions_from_json(o.get("actions")),
                )
                for o in data["orphans"]
            ],
            hidden=data["hidden"],
            next_cursor=data.get("next_cursor"),
        )

    def iter(self, environment: str, *, page_size: int = 100) -> Iterator[Orphan]:
        """Yield every orphan visible to the caller, fetching pages as needed.

        Parameters
        ----------
        environment
            Environment name.
        page_size
            Orphans per request (1 to 200).

        Returns
        -------
        Iterator of Orphan
            Each orphan, once, in repository and table order.
        """
        return every_page(
            lambda cursor: _OrphanPage(
                (page := self.list(environment, limit=page_size, cursor=cursor)).orphans,
                page.next_cursor,
            )
        )


class _OrphanPage(NamedTuple):
    """The rows and next cursor of one orphans page, in the shape `every_page` reads.

    Attributes
    ----------
    items
        The orphans of the page.
    next_cursor
        The cursor of the next page, or `None`.
    """

    items: list[Orphan]
    next_cursor: str | None
