"""FastAPI application construction."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import flush_suppressed
from lakana_core.audit.content import install_query_text
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.bootstrap import bootstrap_if_needed
from lakana_core.ci_oidc.verification import IdTokenVerifier
from lakana_core.config import warn_about_tokens_in_url_paths
from lakana_core.db import create_engine
from lakana_core.grants.admin import ineffective_grants_warning
from lakana_core.readiness import probe_storage, read_database
from lakana_core.secrets.crypto import ACCEPT_FORMAT_1_WARNING
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_server.api.iceberg.router import router as iceberg_router
from lakana_server.api.platform.artifacts import MANIFEST_DECODE_SLOTS, MAX_CONCURRENT_DOWNLOADS
from lakana_server.api.platform.artifacts import environment_router as artifacts_environment_router
from lakana_server.api.platform.artifacts import router as artifacts_router
from lakana_server.api.platform.audit_events import router as audit_events_router
from lakana_server.api.platform.ci_oidc import router as ci_oidc_router
from lakana_server.api.platform.ci_trust_rules import router as ci_trust_rules_router
from lakana_server.api.platform.column_masks import router as column_masks_router
from lakana_server.api.platform.connections import router as connections_router
from lakana_server.api.platform.deploys import router as deploys_router
from lakana_server.api.platform.environments import router as environments_router
from lakana_server.api.platform.grants import router as grants_router
from lakana_server.api.platform.groups import router as groups_router
from lakana_server.api.platform.info import router as info_router
from lakana_server.api.platform.jobs import router as jobs_router
from lakana_server.api.platform.namespaces import router as namespaces_router
from lakana_server.api.platform.package_indexes import router as package_indexes_router
from lakana_server.api.platform.principals import router as principals_router
from lakana_server.api.platform.queries import router as queries_router
from lakana_server.api.platform.repositories import router as repositories_router
from lakana_server.api.platform.row_filters import router as row_filters_router
from lakana_server.api.platform.runs import router as runs_router
from lakana_server.api.platform.runs import scheduler_router
from lakana_server.api.platform.search import router as search_router
from lakana_server.api.platform.secrets import router as secrets_router
from lakana_server.api.platform.tables import dropped_router as dropped_tables_router
from lakana_server.api.platform.tables import expiring_router as expiring_tables_router
from lakana_server.api.platform.tables import router as tables_router
from lakana_server.api.platform.task_connections import router as task_connections_router
from lakana_server.api.platform.task_tokens import router as task_tokens_router
from lakana_server.api.platform.tokens import router as tokens_router
from lakana_server.api.platform.whoami import router as whoami_router
from lakana_server.api.routing import describe_openapi
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.body_limit import BodyLimitMiddleware, streaming_routes
from lakana_server.concurrency import AppendConcurrencyLimiter
from lakana_server.deprecation import DeprecationHeaders
from lakana_server.errors import install_exception_handlers
from lakana_server.readiness import ReadinessCheck
from lakana_server.settings import Settings
from lakana_server.sign_in_audit import SignInAuditLimiter

logger = logging.getLogger(__name__)

ROUTERS = (
    environments_router,
    namespaces_router,
    queries_router,
    tables_router,
    dropped_tables_router,
    expiring_tables_router,
    search_router,
    artifacts_router,
    artifacts_environment_router,
    iceberg_router,
    principals_router,
    groups_router,
    tokens_router,
    task_tokens_router,
    grants_router,
    row_filters_router,
    column_masks_router,
    secrets_router,
    connections_router,
    task_connections_router,
    repositories_router,
    package_indexes_router,
    deploys_router,
    runs_router,
    jobs_router,
    scheduler_router,
    audit_events_router,
    ci_trust_rules_router,
    ci_oidc_router,
    whoami_router,
    info_router,
)
"""Every router the application includes, in the order they are included."""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the Lakana control-plane application.

    Parameters
    ----------
    settings
        Configuration to build the app with. When `None`, `Settings()` reads
        from the environment (the `LAKANA_` prefix).

    Returns
    -------
    FastAPI
        The application, with its lifespan, routers, and exception handlers
        installed.
    """
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Build the engine and run lockout-recovery bootstrap on startup.

        Parameters
        ----------
        app
            The application whose `state` this attaches the engine to.

        Yields
        ------
        None
            Control returns to FastAPI for the app's serving lifetime; the
            denial counts not yet recorded are written as summary rows and the
            engine is disposed when the lifespan exits, even if the startup
            bootstrap below raises. Also names, in one WARNING, every
            single-action grant of `MANAGE_GRANT`, `MANAGE_GROUP` or
            `MANAGE_TOKEN`: such a grant has no effect, because those actions
            work only with the `ADMIN` role. A database error while looking
            for them is logged and start-up goes on.
        """
        warn_about_tokens_in_url_paths(
            logger,
            {
                "LAKANA_RUN__INDEX_URL": resolved_settings.run.index_url,
                "LAKANA_RUN__HTTPS_PROXY": resolved_settings.run.https_proxy,
            },
        )
        if resolved_settings.secrets.accept_format_1:
            logger.warning(ACCEPT_FORMAT_1_WARNING)
        engine = create_engine(
            resolved_settings.database, lease_seconds=resolved_settings.run.lease_seconds
        )
        app.state.engine = engine
        app.state.settings = resolved_settings
        install_query_text(resolved_settings.audit.query_text)
        app.state.auth_audit_limiter = AuthAuditLimiter(
            resolved_settings.auth_audit.per_ip_per_minute
        )
        # Its own bucket: a flood of refused exchanges must not hide failed sign-ins.
        app.state.oidc_exchange_audit_limiter = AuthAuditLimiter(
            resolved_settings.auth_audit.per_ip_per_minute
        )
        # One verifier per process: it holds the issuers' key cache.
        app.state.ci_oidc_verifier = IdTokenVerifier(resolved_settings.ci_oidc)
        app.state.sign_in_audit_limiter = SignInAuditLimiter(
            resolved_settings.audit.sign_in_window_minutes * 60.0
        )
        app.state.denial_audit_limiter = DenialAuditLimiter(
            resolved_settings.denial_audit.per_principal_per_minute
        )
        app.state.append_concurrency_limiter = AppendConcurrencyLimiter(
            resolved_settings.append.max_concurrent
        )
        app.state.manifest_decode_slots = asyncio.Semaphore(MANIFEST_DECODE_SLOTS)
        app.state.manifest_principals = set()
        app.state.download_slots = AppendConcurrencyLimiter(MAX_CONCURRENT_DOWNLOADS)
        readiness = resolved_settings.readiness
        app.state.readiness = ReadinessCheck(
            read=functools.partial(
                read_database,
                engine,
                lease_seconds=resolved_settings.run.lease_seconds,
                environments=readiness.environments,
                key_provider=EnvKeyProvider(
                    resolved_settings.secrets.master_key,
                    accept_format_1=resolved_settings.secrets.accept_format_1,
                ),
            ),
            probe=probe_storage,
            timeout_seconds=readiness.timeout_seconds,
            cache_seconds=readiness.cache_seconds,
        )

        try:
            with Session(engine) as session:
                token = bootstrap_if_needed(
                    session,
                    resolved_settings.bootstrap_admin,
                    key_id=resolved_settings.bootstrap_pat_key_id,
                    secret=resolved_settings.bootstrap_pat_secret,
                    token_file=resolved_settings.bootstrap_token_file,
                )
            if token is not None:
                configured_key_id = resolved_settings.bootstrap_pat_key_id
                if configured_key_id is not None and (
                    resolved_settings.bootstrap_pat_secret is not None
                ):
                    # The caller (e.g. `lakana server start --local`, the Compose bundle)
                    # supplied this credential and already knows the full token. The normal
                    # first start of a local instance, and of a restored Compose instance:
                    # nothing for the reader to act on, so DEBUG.
                    logger.debug(
                        "Created the first administrator %r with the access token whose key "
                        "id %r was supplied at start-up; the token is not logged.",
                        resolved_settings.bootstrap_admin,
                        configured_key_id,
                    )
                else:
                    # The token itself is in the file `bootstrap_if_needed` wrote; it never
                    # reaches a log (#208). The key id is the third `_`-separated part.
                    logger.warning(
                        "Created the first administrator %r. Its access token (key id %s) was "
                        "written to %s; read it there, sign in with it, then delete the file.",
                        resolved_settings.bootstrap_admin,
                        token.split("_", 3)[2],
                        resolved_settings.bootstrap_token_file,
                    )
            try:
                with Session(engine) as session:
                    ineffective = ineffective_grants_warning(session)
            except SQLAlchemyError:
                logger.warning(
                    "The grants that have no effect could not be listed at start-up (a "
                    "database error); start-up continues. `lakana grant list` marks them "
                    "with a note once the database answers.",
                    exc_info=True,
                )
            else:
                if ineffective is not None:
                    logger.warning(ineffective)
            yield
        finally:
            try:
                with Session(engine) as session:
                    flush_suppressed(app.state.denial_audit_limiter, session)
            finally:
                engine.dispose()

    app = FastAPI(lifespan=lifespan)
    install_exception_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Report that the process answers HTTP, with no authentication.

        Used by `lakana server start --local` for readiness and by container
        healthchecks. It reveals nothing beyond liveness.

        Returns
        -------
        dict of str to str
            `{"status": "ok"}`.
        """
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """Report whether the instance can do work, with no authentication.

        Ready means the catalog database answers, every checked environment's storage answers
        (`LAKANA_READINESS__ENVIRONMENTS`), and at least one worker has written a heartbeat
        within `LAKANA_RUN__LEASE_SECONDS`. The answer names the failed checks and nothing
        else, and is reused for `LAKANA_READINESS__CACHE_SECONDS`
        (`lakana_server.readiness`).

        Parameters
        ----------
        request
            The current request, used to reach `app.state.readiness`.

        Returns
        -------
        JSONResponse
            200 `{"status": "ready"}`, or 503 `{"status": "not_ready", "failed": [...]}` with
            `database`, `storage` or `worker`; never cached by a proxy.
        """
        failed = await request.app.state.readiness.failed()
        headers = {"Cache-Control": "no-store"}
        if not failed:
            return JSONResponse({"status": "ready"}, headers=headers)
        return JSONResponse(
            {"status": "not_ready", "failed": list(failed)}, status_code=503, headers=headers
        )

    for router in ROUTERS:
        app.include_router(router)
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=resolved_settings.request_body.max_bytes,
        exempt=streaming_routes(ROUTERS),
        iceberg=iceberg_router.routes,
    )
    app.add_middleware(DeprecationHeaders)
    describe_openapi(app, ROUTERS)
    return app
