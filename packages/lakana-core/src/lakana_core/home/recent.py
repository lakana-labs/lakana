"""Recently opened: the tables, queries, jobs and runs a person opened, kept on the server.

The detail pages of the web interface record an open; Home lists them, so the list follows the
person to another browser. Self-service and not audited, like the interface state
(`lakana_core.identity.interface_state`). An object is recorded only when it exists and the
caller may read it, and listed only while both still hold: a revoked grant or a dropped table
makes its entry disappear, never a refusal, and nothing about it is shown. Every check is
`permits`, which records nothing.
"""

from __future__ import annotations

import builtins
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast, get_args

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.identity.interface_state import require_self
from lakana_core.identity.sessions import utc_now
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    QueryRun,
    RecentOpen,
    Repository,
    Run,
)
from lakana_core.projects.manifest import manifest_jobs

RECENT_KEEP = 20
"""How many entries a person keeps; recording a new one drops the oldest beyond it."""

RECENT_DEBOUNCE = timedelta(seconds=60)
"""Opening the same object again within this time writes nothing."""

RECENT_REF_BYTES = 1024
"""The longest reference stored, in UTF-8 bytes."""

RecentKind = Literal["table", "query", "job", "run"]
"""What can be recently opened."""

_KINDS: tuple[str, ...] = get_args(RecentKind)

_WHAT = "recently opened list"


@dataclass(frozen=True)
class RecentView:
    """One recently opened object.

    Parameters
    ----------
    kind
        ``table``, ``query``, ``job`` or ``run``.
    environment
        The environment's name.
    ref
        The object: a dotted table name (``gold.sales``), a query's or a run's id, or
        ``repository/job``.
    opened_at
        When the person last opened it.
    """

    kind: RecentKind
    environment: str
    ref: str
    opened_at: datetime


def _existing(
    session: Session, principal_id: uuid.UUID, kind: str, environment_id: uuid.UUID, ref: str
) -> RecentOpen | None:
    """Find the entry of this object for this person.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The person.
    kind
        The object's kind.
    environment_id
        Its environment.
    ref
        Its reference.

    Returns
    -------
    RecentOpen or None
        The entry, if any.
    """
    return session.execute(
        select(RecentOpen).where(
            RecentOpen.principal_id == principal_id,
            RecentOpen.kind == kind,
            RecentOpen.environment_id == environment_id,
            RecentOpen.ref == ref,
        )
    ).scalar_one_or_none()


def _as_uuid(ref: str) -> uuid.UUID | None:
    """Read an id.

    Parameters
    ----------
    ref
        The reference sent.

    Returns
    -------
    uuid.UUID or None
        The id, or `None` for anything that is not one.
    """
    try:
        return uuid.UUID(ref)
    except ValueError:
        return None


class RecentService:
    """Record and list the objects a person opened.

    Parameters
    ----------
    session
        Database session; `record` commits.
    authz
        Authorization service, asked with `permits` only.
    clock
        The time an open is recorded at; the sign-in services' clock by default.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._authz = authz
        self._clock = clock

    def record(self, p: PrincipalRef, name: str, *, kind: str, environment: str, ref: str) -> None:
        """Record that the person opened an object.

        Opening the same object again moves it to the top, except within `RECENT_DEBOUNCE` of
        the last time, which writes nothing; only the newest `RECENT_KEEP` entries are kept.

        Parameters
        ----------
        p
            The caller.
        name
            The person; must be the caller.
        kind
            ``table``, ``query``, ``job`` or ``run``.
        environment
            The environment's name.
        ref
            The object: a dotted table name, a query's or a run's id, or ``repository/job``.

        Raises
        ------
        PermissionDenied
            For another person's name, or a task or CI token.
        ValidationError
            For an unknown kind, or a reference over `RECENT_REF_BYTES` bytes.
        NotFoundError
            When the object does not exist or the caller may not read it (the same message).
        """
        require_self(p, name, _WHAT)
        if kind not in _KINDS:
            raise ValidationError(
                f"kind: '{shown(kind)}' is not something Home lists; use table, query, job or run."
            )
        if any(unicodedata.category(c) == "Cc" for c in environment + ref):
            raise ValidationError(
                "environment and ref: a control character is in no name; send the name as the "
                "page shows it."
            )
        if len(ref.encode()) > RECENT_REF_BYTES:
            raise ValidationError(f"ref: at most {RECENT_REF_BYTES} bytes.")
        environment_id = self._readable(p, kind, environment, ref)
        if environment_id is None:
            raise NotFoundError(
                f"No {kind} {shown(ref)} in {shown(environment)} that you can read."
            )
        now = self._clock()
        row = _existing(self._session, p.id, kind, environment_id, ref)
        if row is not None:
            if now - row.opened_at >= RECENT_DEBOUNCE:
                row.opened_at = now
                self._session.commit()
            return
        self._session.add(
            RecentOpen(
                id=uuid.uuid4(),
                principal_id=p.id,
                kind=kind,
                environment_id=environment_id,
                ref=ref,
                opened_at=now,
            )
        )
        try:
            self._session.flush()
        except IntegrityError:
            # Another request of the same person recorded the same object first; one row is
            # what both meant.
            self._session.rollback()
            return
        kept = (
            select(RecentOpen.id)
            .where(RecentOpen.principal_id == p.id)
            .order_by(RecentOpen.opened_at.desc(), RecentOpen.id.desc())
            .limit(RECENT_KEEP)
        )
        self._session.execute(
            delete(RecentOpen).where(
                RecentOpen.principal_id == p.id, RecentOpen.id.not_in(kept.scalar_subquery())
            )
        )
        self._session.commit()

    def list(self, p: PrincipalRef, name: str) -> builtins.list[RecentView]:
        """List what the person opened, newest first.

        Parameters
        ----------
        p
            The caller.
        name
            The person; must be the caller.

        Returns
        -------
        list of RecentView
            At most `RECENT_KEEP`; an entry whose object no longer exists, whose environment was
            deleted, or that the caller may no longer read is left out.

        Raises
        ------
        PermissionDenied
            For another person's name, or a task or CI token.
        """
        require_self(p, name, _WHAT)
        rows = self._session.execute(
            select(RecentOpen, Environment.name)
            .join(Environment, Environment.id == RecentOpen.environment_id)
            .where(RecentOpen.principal_id == p.id, LIVE_ENVIRONMENT)
            .order_by(RecentOpen.opened_at.desc(), RecentOpen.id.desc())
            .limit(RECENT_KEEP)
        ).all()
        return [
            RecentView(
                kind=cast("RecentKind", row.kind),
                environment=env,
                ref=row.ref,
                opened_at=row.opened_at,
            )
            for row, env in rows
            if self._found(p, row.kind, env, row.environment_id, row.ref)
        ]

    def _readable(self, p: PrincipalRef, kind: str, environment: str, ref: str) -> uuid.UUID | None:
        """Find the object's environment when the object exists and the caller may read it.

        Parameters
        ----------
        p
            The caller.
        kind
            The object's kind.
        environment
            Its environment's name.
        ref
            Its reference.

        Returns
        -------
        uuid.UUID or None
            The environment's id, or `None` for a missing or unreadable object.
        """
        env_id = self._session.execute(
            select(Environment.id).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar()
        if env_id is None or not self._found(p, kind, environment, env_id, ref):
            return None
        return env_id

    def _found(
        self, p: PrincipalRef, kind: str, environment: str, env_id: uuid.UUID, ref: str
    ) -> bool:
        """Say whether the object exists in a live environment and the caller may read it.

        Parameters
        ----------
        p
            The caller.
        kind
            The object's kind.
        environment
            Its environment's name.
        env_id
            Its environment's id.
        ref
            Its reference.

        Returns
        -------
        bool
            Whether it is readable.
        """
        if kind == "table":
            return self._table(p, environment, ref)
        if kind == "query":
            return self._query(p, environment, env_id, ref)
        if kind == "job":
            return self._job(p, environment, env_id, ref)
        return self._run(p, environment, env_id, ref)

    def _table(self, p: PrincipalRef, environment: str, ref: str) -> bool:
        """Say whether the table exists and the caller may read it.

        The right is asked before the table is looked up, so a table the caller may not read
        and one that does not exist cost and answer the same.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name.
        ref
            The dotted table name.

        Returns
        -------
        bool
            Whether it is readable.
        """
        try:
            table = TableRef.from_dotted(environment, ref)
        except ValidationError:
            return False
        scope = Securable.table_scope(environment, table.namespace, table.name)
        if not self._authz.permits(p, Action.READ_TABLE, scope):
            return False
        joined = table.namespace.joined()
        return resolve_table_id(self._session, environment, joined, table.name) is not None

    def _query(self, p: PrincipalRef, environment: str, env_id: uuid.UUID, ref: str) -> bool:
        """Say whether the query run exists and the caller may read it.

        Only its submitter, while they may still submit queries there: the query's own page
        (`QueryService.get_query_run`) shows it to nobody else, so Home lists nothing else.
        `READ_QUERY_TEXT` opens query text in the audit log, not the query.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name.
        env_id
            Its id.
        ref
            The query run's id.

        Returns
        -------
        bool
            Whether it is readable.
        """
        query_id = _as_uuid(ref)
        if query_id is None:
            return False
        if not self._authz.permits(
            p, Action.SUBMIT_QUERY, Securable.environment_scope(environment)
        ):
            return False
        found = self._session.execute(
            select(QueryRun.id).where(
                QueryRun.id == query_id,
                QueryRun.environment_id == env_id,
                QueryRun.principal_id == p.id,
            )
        ).first()
        return found is not None

    def _job(self, p: PrincipalRef, environment: str, env_id: uuid.UUID, ref: str) -> bool:
        """Say whether a deployed repository declares the job and the caller may read it.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name.
        env_id
            Its id.
        ref
            ``repository/job``.

        Returns
        -------
        bool
            Whether it is readable.
        """
        repository, slash, job = ref.partition("/")
        if not slash or not self._authz.permits(
            p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
        ):
            return False
        manifest = self._session.execute(
            select(JobVersion.manifest_json)
            .join(
                EnvironmentDeployment, EnvironmentDeployment.current_job_version_id == JobVersion.id
            )
            .join(Repository, Repository.id == EnvironmentDeployment.repository_id)
            .where(EnvironmentDeployment.environment_id == env_id, Repository.name == repository)
        ).scalar()
        return job in manifest_jobs(manifest)

    def _run(self, p: PrincipalRef, environment: str, env_id: uuid.UUID, ref: str) -> bool:
        """Say whether the run exists and the caller may read it.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name.
        env_id
            Its id.
        ref
            The run's id.

        Returns
        -------
        bool
            Whether it is readable.
        """
        run_id = _as_uuid(ref)
        scope = Securable.environment_scope(environment)
        # As the run's own page: `RUN` or `READ_ENVIRONMENT` (`runs.service.authorize_run_read`).
        if run_id is None or not (
            self._authz.permits(p, Action.RUN, scope)
            or self._authz.permits(p, Action.READ_ENVIRONMENT, scope)
        ):
            return False
        found = self._session.execute(
            select(Run.id).where(Run.id == run_id, Run.environment_id == env_id)
        ).first()
        return found is not None
