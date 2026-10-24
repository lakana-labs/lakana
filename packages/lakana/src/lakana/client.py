"""The SDK's top-level entry point."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Literal, overload

import httpx
import pyarrow as pa

from lakana._ci_oidc import (
    AUDIENCE_VARIABLE,
    GITLAB_ONE_SUBMISSION,
    id_token_for,
    resolve_provider,
)
from lakana._config import resolve_config
from lakana._http import HttpTransport
from lakana._models import (
    CiToken,
    CiTokenScope,
    InstanceInfo,
    QueryStatus,
    SearchHit,
    SearchPage,
    TaskTokenScope,
    Whoami,
)
from lakana._routes import ROUTES
from lakana._tokens import mask_token
from lakana.errors import AuthenticationError, ConfigError, InvalidResponse
from lakana.resources.artifacts import Artifacts
from lakana.resources.audit_events import AuditEvents
from lakana.resources.checks import Checks
from lakana.resources.ci_trust_rules import CiTrustRules, exchange_id_token
from lakana.resources.column_masks import ColumnMasks
from lakana.resources.connections import Connections
from lakana.resources.deploys import Deployments, Deploys, OrphanTables, ProjectVersions
from lakana.resources.environments import Environments
from lakana.resources.grants import Grants
from lakana.resources.groups import Groups
from lakana.resources.jobs import Jobs
from lakana.resources.namespaces import Namespaces
from lakana.resources.package_indexes import PackageIndexes
from lakana.resources.principals import Principals
from lakana.resources.queries import Queries
from lakana.resources.repositories import Repositories
from lakana.resources.row_filters import RowFilters
from lakana.resources.runs import Runs, Scheduler, Workers
from lakana.resources.secrets import Secrets
from lakana.resources.tables import Tables
from lakana.resources.task_connections import TaskConnections
from lakana.resources.task_tokens import TaskTokens
from lakana.resources.tokens import Tokens


class Client:
    """The Lakana SDK's entry point.

    Parameters
    ----------
    url
        The platform API's base URL. See `lakana._config.resolve_config` for
        how this is resolved when omitted.
    token
        A PAT or task token.
    profile
        A named profile to select from the profile file, when `url`/`token`
        are not given explicitly and no `LAKANA_URL`/`LAKANA_TOKEN` are set.
    timeout
        Request timeout in seconds.
    verify
        TLS verification setting: `True` (the default), `False`, or a CA
        bundle path.
    http_client
        An existing `httpx.Client` to use instead of building one — for
        example in tests, or Starlette's `TestClient`.
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        profile: str | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = resolve_config(url, token, profile)
        self.ci_token: CiToken | None = None
        """The CI token's details when `from_ci_oidc` built this client; never shows the token."""
        self._transport = HttpTransport(
            self._config, timeout=timeout, verify=verify, http_client=http_client
        )
        self.environments = Environments(self._transport)
        self.namespaces = Namespaces(self._transport)
        self.tables = Tables(self._transport)
        self.artifacts = Artifacts(self._transport)
        self.queries = Queries(self._transport)
        self.principals = Principals(self._transport)
        self.groups = Groups(self._transport)
        self.tokens = Tokens(self._transport)
        self.task_tokens = TaskTokens(self._transport)
        self.grants = Grants(self._transport)
        self.row_filters = RowFilters(self._transport)
        self.column_masks = ColumnMasks(self._transport)
        self.secrets = Secrets(self._transport)
        self.connections = Connections(self._transport)
        self.task_connections = TaskConnections(self._transport)
        """For the helpers of `lakana.ingest` inside a task: the fetch of a declared connection."""
        self.repositories = Repositories(self._transport)
        self.package_indexes = PackageIndexes(self._transport)
        self.ci_trust_rules = CiTrustRules(self._transport)
        self.audit_events = AuditEvents(self._transport)
        self.deploys = Deploys(self._transport)
        self.deployments = Deployments(self._transport)
        self.project_versions = ProjectVersions(self._transport)
        self.orphans = OrphanTables(self._transport)
        self.runs = Runs(self._transport)
        self.jobs = Jobs(self._transport)
        self.checks = Checks(self._transport)
        self.scheduler = Scheduler(self._transport)
        self.workers = Workers(self._transport)

    @classmethod
    def from_ci_oidc(
        cls,
        url: str,
        *,
        environment: str,
        repository: str,
        provider: Literal["github", "gitlab"] | None = None,
        id_token: str | None = None,
        audience: str | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
        http_client: httpx.Client | None = None,
    ) -> Client:
        """Build a client for a CI job from its OIDC ID token, without a stored secret.

        The job's ID token is exchanged for a CI token (``lakana_ci_…``) that may queue one
        deploy of the job's own commit, or one dry run, of `repository` to `environment`, and
        then follow that run. Each client needs a new exchange: build one per submission. The
        ID token and the CI token stay in memory; neither is written, logged or shown.

        Parameters
        ----------
        url
            The Lakana server's base URL.
        environment
            The environment the job deploys to.
        repository
            The registered repository it deploys.
        provider
            ``github`` asks the GitHub Actions runner for a new ID token (the job needs
            ``permissions: id-token: write``); ``gitlab`` reads ``LAKANA_OIDC_TOKEN``, which the
            job's ``id_tokens:`` entry sets. `None` reads ``LAKANA_OIDC``, and without it takes
            the ID token from ``LAKANA_OIDC_TOKEN``.
        id_token
            The ID token itself; wins over `provider`.
        audience
            The audience to ask GitHub for: the server's ``LAKANA_CI_OIDC__AUDIENCE``. By
            default ``LAKANA_OIDC_AUDIENCE``, and without it `url` exactly as given.
        timeout
            Request timeout in seconds.
        verify
            TLS verification for the Lakana server: `True`, `False` or a CA bundle path.
        http_client
            An existing `httpx.Client` to send through, for example in tests.

        Returns
        -------
        Client
            A client authenticated with the CI token; `ci_token` describes it.

        Raises
        ------
        lakana.errors.ConfigError
            If no ID token can be found; the message names the variable or permission to add.
        lakana.errors.AuthenticationError
            If the provider or the server refuses the ID token; the message says why.
        lakana.errors.PermissionDenied
            If no single enabled CI trust rule accepts it for this environment and repository.
        """
        chosen = resolve_provider(provider, os.environ)
        if id_token is not None:
            token = id_token.strip()
            if not token:
                raise ConfigError("id_token is empty: pass the CI job's ID token, or leave it out.")
        else:
            token = id_token_for(
                chosen,
                audience=audience or os.environ.get(AUDIENCE_VARIABLE) or url,
                environ=os.environ,
                timeout=timeout,
                http_client=http_client,
            )
        try:
            issued = exchange_id_token(
                url,
                token,
                environment=environment,
                repository=repository,
                timeout=timeout,
                verify=verify,
                http_client=http_client,
            )
        except AuthenticationError as exc:
            if chosen == "github" or "exchanged before" not in str(exc):
                raise
            raise AuthenticationError(
                f"{exc}{GITLAB_ONE_SUBMISSION}",
                status=exc.status,
                error_type=exc.error_type,
                retry_after=exc.retry_after,
            ) from None
        client = cls(
            url=url, token=issued.token, timeout=timeout, verify=verify, http_client=http_client
        )
        client.ci_token = issued
        return client

    @property
    def url(self) -> str:
        """The platform base URL this client talks to.

        Returns
        -------
        str
            The resolved URL.
        """
        return self._config.url

    @overload
    def sql(
        self,
        sql: str,
        environment: str,
        timeout: float | None = None,
        *,
        into: None = None,
        mode: None = None,
        incremental_key: str | None = None,
        evolve_schema: Literal[False] = False,
        break_glass_reason: None = None,
    ) -> pa.Table: ...

    @overload
    def sql(
        self,
        sql: str,
        environment: str,
        timeout: float | None = None,
        *,
        into: str,
        mode: str,
        incremental_key: str | None = None,
        evolve_schema: bool = False,
        break_glass_reason: str | None = None,
    ) -> QueryStatus: ...

    def sql(
        self,
        sql: str,
        environment: str,
        timeout: float | None = None,
        *,
        into: str | None = None,
        mode: str | None = None,
        incremental_key: str | None = None,
        evolve_schema: bool = False,
        break_glass_reason: str | None = None,
    ) -> pa.Table | QueryStatus:
        """Submit a query, wait for it to finish, and return its result or its write status.

        Parameters
        ----------
        sql
            The SQL text to run.
        environment
            The environment to run the query against.
        timeout
            Seconds to wait before giving up, or `None` to wait
            indefinitely. Passed straight through to `QueryRun.wait`.
        into
            Write the result into this table or view instead.
        mode
            `create`, `replace`, `append` or `view`; required with `into`.
        incremental_key
            With `mode="append"`, append only rows past the target's maximum
            of this column.
        evolve_schema
            With `replace`/`append`, let the target adopt the result's schema.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Returns
        -------
        pa.Table or QueryStatus
            The result, or — with `into` — the finished run's status, which
            carries `rows_written`, `snapshot_id`, `watermark` and
            `schema_diff` (a write has no result set).

        Raises
        ------
        lakana.errors.QueryFailed
            If the query fails.
        lakana.errors.ValidationError
            If the write options don't go together.
        TimeoutError
            If `timeout` elapses before the query finishes.
        """
        run = self.queries.submit(
            sql,
            environment,
            into=into,
            mode=mode,
            incremental_key=incremental_key,
            evolve_schema=evolve_schema,
            break_glass_reason=break_glass_reason,
        )
        status = run.wait(timeout=timeout)
        return run.result() if into is None else status

    def whoami(self) -> Whoami:
        """Describe who this client's credential authenticates as.

        Returns
        -------
        Whoami
            The principal's name and type, and its scope if the credential is a task token
            or a CI token.

        Raises
        ------
        lakana.errors.AuthenticationError
            If the credential is invalid, expired or revoked.
        """
        body = self._transport.request(ROUTES["whoami"]).json()
        scope = body["task_token"]
        ci = body.get("ci_token")
        return Whoami(
            name=body["name"],
            type=body["type"],
            task_token=None
            if scope is None
            else TaskTokenScope(environment=scope["environment"], asset_scope=scope["asset_scope"]),
            ci_token=None
            if ci is None
            else CiTokenScope(
                rule_id=ci["rule_id"],
                environment=ci["environment"],
                repository=ci["repository"],
                kinds=list(ci["kinds"]),
                sha=ci["sha"],
                expires_at=None
                if ci["expires_at"] is None
                else datetime.fromisoformat(ci["expires_at"]),
                used=bool(ci["used"]),
            ),
        )

    def search(
        self,
        environment: str,
        q: str,
        *,
        kind: Literal["table", "column", "run", "principal"] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchPage:
        """Find tables, columns, descriptions, runs and principals by a text.

        Matching ignores case and finds the text anywhere in a name or description. Exact matches
        come first, then names that start with the text, then names that contain it, then
        descriptions; within each, tables come before columns, runs and principals. You see only
        what you may read: tables and columns with ``READ_TABLE`` on the table, runs with
        ``READ_ENVIRONMENT`` (found by the first four or more characters of the id, the job or the
        repository), principals if you may list them.

        Parameters
        ----------
        environment
            The environment name.
        q
            The text, 1 to 200 characters after trimming.
        kind
            Only this kind of hit: ``table``, ``column``, ``run`` or ``principal``.
        limit
            Hits per page, 1 to 100.
        offset
            Hits to skip, for the next page.

        Returns
        -------
        SearchPage
            The page; ``has_more`` says whether to ask again with a larger ``offset``.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If the text, ``limit`` or ``offset`` is out of range; the message names the range.
        """
        params: dict[str, str | int] = {"q": q, "limit": limit, "offset": offset}
        if kind is not None:
            params["kind"] = kind
        body = self._transport.request(
            ROUTES["search"], path_params={"env": environment}, params=params
        ).json()
        return SearchPage(
            results=[SearchHit(**hit) for hit in body["results"]],
            limit=body["limit"],
            offset=body["offset"],
            has_more=body["has_more"],
        )

    def info(self) -> InstanceInfo:
        """Ask the instance which platform version it runs.

        The request is sent once, without the transport's retries, so a dead server fails fast.

        Returns
        -------
        InstanceInfo
            The instance's version.

        Raises
        ------
        lakana.errors.AuthenticationError
            If the credential is invalid, expired or revoked.
        lakana.errors.NotFound
            If the server predates the endpoint.
        lakana.errors.InvalidResponse
            If the URL answers with something that is not Lakana's JSON.
        """
        response = self._transport.request(ROUTES["info"], retry=False)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("version"), str):
            raise InvalidResponse(f"{self.url} did not answer like a Lakana server")
        return InstanceInfo(version=body["version"])

    def close(self) -> None:
        """Close the underlying HTTP transport."""
        self._transport.close()

    def __enter__(self) -> Client:
        """Return this client for use as a context manager.

        Returns
        -------
        Client
            This client.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the client on exit from a `with` block.

        Parameters
        ----------
        *exc
            The exception type, value, and traceback, if the block raised
            one. Unused.
        """
        self.close()

    def __repr__(self) -> str:
        """Return a repr with the token masked.

        Returns
        -------
        str
            `"Client(url='...', token='lakana_pat_<key>_***')"`, never
            including the token's secret part.
        """
        return f"Client(url={self._config.url!r}, token={mask_token(self._config.token)!r})"
