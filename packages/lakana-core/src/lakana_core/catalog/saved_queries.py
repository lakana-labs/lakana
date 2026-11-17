"""Saved queries: SQL text a person keeps under a name, private or shared in its environment.

A saved query is only text and an environment. Running it is an ordinary query that whoever runs
it submits with their own rights; it never runs with its author's.

Who sees a saved query in a live environment:

- its owner, always, with or without `SUBMIT_QUERY` there today (so they can clean up);
- for a shared (``everyone``) one, whoever holds `SUBMIT_QUERY` on its environment, so nobody
  sees a query they cannot run;
- an instance administrator (personal token or session), every one: the SQL of a private one
  only with `READ_QUERY_TEXT` on the instance as well.

Anyone else gets the same 404 as for a missing one. A deleted environment hides its saved
queries, a restore shows them again, and a purge removes them. Only the owner updates, and only
while they hold `SUBMIT_QUERY` there; the owner or an administrator deletes. A task's token is
refused everything. Each change is audited in its own transaction, never with the SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.errors import AlreadyExistsError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.admin import acts_as_instance_admin
from lakana_core.identifiers import hidden_character
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    AuditEventType,
    Environment,
    Principal,
    SavedQuery,
    SavedQueryVisibility,
)
from lakana_core.pagination import Page, page_in_order

SAVED_QUERY_NAME_MAX = 200
"""The most characters of a saved query's name, after stripping."""

SAVED_QUERY_LIST_DEFAULT = 50
"""The page size of a saved-query list when the caller names none."""

SAVED_QUERY_LIST_MAX = 200
"""The largest page size of a saved-query list."""

TASK_TOKEN_REFUSED = (
    "Saved queries belong to people and service accounts; a task's token cannot read or change "
    "them."
)
"""Why every saved-query operation refuses a task's token."""

NAME_REFUSED = "A saved query's name is one line of 1 to 200 visible characters."
"""Why a name is refused: empty after stripping, too long, or holding a hidden character."""

SQL_EMPTY = "A saved query needs SQL; the text sent is empty."
"""Why SQL that is empty after stripping is refused."""

ONLY_OWNER_UPDATES = (
    "Only the owner of a saved query can change it; save a copy under your own name instead."
)
"""Why an update by anyone but the owner is refused."""

ONLY_OWNER_OR_ADMIN_DELETES = "Only the owner of a saved query or an administrator can delete it."
"""Why a delete by anyone but the owner or an administrator is refused."""

_ONE_ENVIRONMENT, _EVERY_ENVIRONMENT = "saved-queries", "all-saved-queries"
"""The cursor kinds of the per-environment list and of the cross-environment list."""

_OWNER_ACTIONS = ("update", "delete")
_DELETE_ONLY = ("delete",)


@dataclass(frozen=True)
class SavedQueryView:
    """A saved query as one caller sees it.

    Parameters
    ----------
    id
        The saved query's id.
    environment
        The name of its environment.
    name
        Its name.
    sql
        Its SQL, or `None` when the caller may not read it: an administrator's view of someone
        else's private query without `READ_QUERY_TEXT`.
    visibility
        ``private`` or ``everyone``.
    owner
        The owner's name, or `None` once the owner's principal is deleted.
    former_owner
        Whether the owner's principal is deleted.
    actions
        What the caller may do with it: ``update`` and ``delete`` for the owner (``update`` only
        while they hold `SUBMIT_QUERY` there), ``delete`` for an administrator who is not the
        owner, nothing for anyone else.
    created_at
        When it was created.
    updated_at
        When it was last changed.
    """

    id: uuid.UUID
    environment: str
    name: str
    sql: str | None
    visibility: str
    owner: str | None
    former_owner: bool
    actions: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class _Reader:
    """What one caller may see and do among the saved queries of some environments.

    Parameters
    ----------
    p
        The caller.
    admin
        Whether the caller acts as an instance administrator.
    queryable
        The ids of the environments where the caller holds `SUBMIT_QUERY`.
    reads_text
        Asks whether the caller holds `READ_QUERY_TEXT` on the instance; asked at most once,
        and only for a private query of someone else.
    """

    def __init__(
        self,
        p: PrincipalRef,
        *,
        admin: bool,
        queryable: frozenset[uuid.UUID],
        reads_text: Callable[[], bool],
    ) -> None:
        self.p = p
        self.admin = admin
        self.queryable = queryable
        self._reads_text = reads_text
        self._text: bool | None = None

    def visible(self) -> ColumnElement[bool]:
        """Keep the saved queries the caller sees, as one SQL condition.

        Returns
        -------
        ColumnElement
            Their own, the shared ones of an environment where they may query, or every one
            for an administrator.
        """
        if self.admin:
            return true()
        return or_(
            SavedQuery.owner_id == self.p.id,
            and_(
                SavedQuery.visibility == SavedQueryVisibility.EVERYONE.value,
                SavedQuery.environment_id.in_(self.queryable),
            ),
        )

    def sees(self, row: SavedQuery) -> bool:
        """Say whether the caller sees one saved query: `visible` for a single row.

        Parameters
        ----------
        row
            The saved query.

        Returns
        -------
        bool
            Whether it is theirs, shared where they may query, or they are an administrator.
        """
        return (
            self.admin
            or row.owner_id == self.p.id
            or (
                row.visibility == SavedQueryVisibility.EVERYONE
                and row.environment_id in self.queryable
            )
        )

    def view(self, row: SavedQuery, environment: str, owner: str | None) -> SavedQueryView:
        """Show a saved query the caller sees.

        Parameters
        ----------
        row
            The saved query.
        environment
            Its environment's name.
        owner
            Its owner's name, or `None` once the owner's principal is deleted.

        Returns
        -------
        SavedQueryView
            The view, its SQL withheld from an administrator without `READ_QUERY_TEXT` reading
            someone else's private query.
        """
        own = row.owner_id == self.p.id
        shows_sql = own or row.visibility == SavedQueryVisibility.EVERYONE or self._text_right()
        if own:
            actions = _OWNER_ACTIONS if row.environment_id in self.queryable else _DELETE_ONLY
        else:
            actions = _DELETE_ONLY if self.admin else ()
        return SavedQueryView(
            id=row.id,
            environment=environment,
            name=row.name,
            sql=row.sql_text if shows_sql else None,
            visibility=row.visibility,
            owner=owner,
            former_owner=row.owner_id is None,
            actions=actions,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _text_right(self) -> bool:
        """Ask once whether the caller holds `READ_QUERY_TEXT` on the instance.

        Returns
        -------
        bool
            The answer, remembered for the rest of the request.
        """
        if self._text is None:
            self._text = self._reads_text()
        return self._text


def _checked_name(name: str) -> str:
    """Strip a saved query's name and check that it is one line of visible characters.

    Parameters
    ----------
    name
        The name as typed.

    Returns
    -------
    str
        The name without surrounding blanks.

    Raises
    ------
    ValidationError
        If it is empty after stripping, longer than `SAVED_QUERY_NAME_MAX`, or holds a control,
        format or surrogate character or a line or paragraph separator.
    """
    stripped = name.strip()
    if not 1 <= len(stripped) <= SAVED_QUERY_NAME_MAX or hidden_character(stripped) is not None:
        raise ValidationError(NAME_REFUSED)
    return stripped


def _checked_fields(name: str, sql: str, visibility: str) -> tuple[str, SavedQueryVisibility]:
    """Check the fields of a saved query a caller sends.

    The SQL's length is the server's to check (`LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`),
    before it calls this service.

    Parameters
    ----------
    name
        The name as typed.
    sql
        The SQL, stored as sent.
    visibility
        ``private`` or ``everyone``.

    Returns
    -------
    tuple of (str, SavedQueryVisibility)
        The stripped name and the visibility.

    Raises
    ------
    ValidationError
        If the name is not one line of 1 to 200 visible characters, the SQL is empty after
        stripping, or the visibility is another word.
    """
    stripped = _checked_name(name)
    if not sql.strip():
        raise ValidationError(SQL_EMPTY)
    try:
        chosen = SavedQueryVisibility(visibility)
    except ValueError:
        raise ValidationError(
            f"visibility is 'private' or 'everyone', not {visibility!r}."
        ) from None
    return stripped, chosen


def _refuse_task_token(p: PrincipalRef) -> None:
    """Refuse a task's token every saved-query operation.

    Parameters
    ----------
    p
        The caller.

    Raises
    ------
    PermissionDenied
        If `p` authenticated with a task token.
    """
    if p.task_scope is not None:
        raise PermissionDenied(TASK_TOKEN_REFUSED)


class SavedQueryService:
    """Create, read, change and delete saved queries, and list them one page at a time.

    Parameters
    ----------
    session
        Database session. Each change commits on success, with its audit event.
    authz
        Authorization service consulted before every operation.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def create(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        name: str,
        sql: str,
        visibility: str = "private",
    ) -> SavedQueryView:
        """Save SQL under a name in an environment, owned by the caller.

        Parameters
        ----------
        p
            The caller, the owner.
        environment
            The environment's name; fixed for the saved query's life.
        name
            The name; stored without surrounding blanks.
        sql
            The SQL, stored as sent.
        visibility
            ``private`` (the default) or ``everyone``.

        Returns
        -------
        SavedQueryView
            The saved query, as its owner sees it.

        Raises
        ------
        PermissionDenied
            If `p` authenticated with a task token, or lacks `SUBMIT_QUERY` on the environment.
        NotFoundError
            If the environment does not exist.
        ValidationError
            If a field is refused (`_checked_fields`).
        AlreadyExistsError
            If the caller already has a saved query of that name there.
        """
        _refuse_task_token(p)
        self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(environment))
        env = self._environment(environment)
        stripped, chosen = _checked_fields(name, sql, visibility)
        self._refuse_duplicate(p, env, stripped, None)
        row = SavedQuery(id=uuid.uuid4(), environment_id=env.id, owner_id=p.id)
        self._session.add(row)
        return self._save(p, env, row, stripped, sql, chosen, AuditEventType.SAVED_QUERY_CREATED)

    def get(self, p: PrincipalRef, environment: str, saved_query_id: uuid.UUID) -> SavedQueryView:
        """Read one saved query the caller sees.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name.
        saved_query_id
            The saved query's id.

        Returns
        -------
        SavedQueryView
            The saved query as the caller sees it.

        Raises
        ------
        PermissionDenied
            If `p` authenticated with a task token.
        NotFoundError
            If the environment does not exist, or holds no such saved query that the caller
            sees.
        """
        _refuse_task_token(p)
        env = self._environment(environment)
        row, owner, reader = self._seen(p, env, saved_query_id)
        return reader.view(row, env.name, owner)

    def update(
        self,
        p: PrincipalRef,
        environment: str,
        saved_query_id: uuid.UUID,
        *,
        name: str,
        sql: str,
        visibility: str,
    ) -> SavedQueryView:
        """Replace the name, SQL and visibility of one of the caller's saved queries.

        The environment never changes; saving in another one is a new `create`.

        Parameters
        ----------
        p
            The caller, who must be the owner.
        environment
            The environment's name.
        saved_query_id
            The saved query's id.
        name
            The new name; stored without surrounding blanks.
        sql
            The new SQL, stored as sent.
        visibility
            ``private`` or ``everyone``.

        Returns
        -------
        SavedQueryView
            The changed saved query, as its owner sees it.

        Raises
        ------
        PermissionDenied
            If `p` authenticated with a task token, is not the owner, or lacks `SUBMIT_QUERY`
            on the environment now.
        NotFoundError
            If the environment does not exist, or holds no such saved query that the caller
            sees.
        ValidationError
            If a field is refused (`_checked_fields`).
        AlreadyExistsError
            If the caller already has another saved query of that name there.
        """
        _refuse_task_token(p)
        env = self._environment(environment)
        row, _, _ = self._seen(p, env, saved_query_id)
        if row.owner_id != p.id:
            raise PermissionDenied(ONLY_OWNER_UPDATES)
        self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(env.name))
        stripped, chosen = _checked_fields(name, sql, visibility)
        self._refuse_duplicate(p, env, stripped, row.id)
        return self._save(p, env, row, stripped, sql, chosen, AuditEventType.SAVED_QUERY_UPDATED)

    def delete(self, p: PrincipalRef, environment: str, saved_query_id: uuid.UUID) -> None:
        """Delete a saved query: the owner's own, or any one the caller sees as administrator.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name.
        saved_query_id
            The saved query's id.

        Raises
        ------
        PermissionDenied
            If `p` authenticated with a task token, or is neither the owner nor an
            administrator.
        NotFoundError
            If the environment does not exist, or holds no such saved query that the caller
            sees.
        """
        _refuse_task_token(p)
        env = self._environment(environment)
        row, owner, reader = self._seen(p, env, saved_query_id)
        if row.owner_id != p.id and not reader.admin:
            raise PermissionDenied(ONLY_OWNER_OR_ADMIN_DELETES)
        self._record(p, env, row, owner, AuditEventType.SAVED_QUERY_DELETED)
        self._session.delete(row)
        self._session.commit()

    def list_page(
        self,
        p: PrincipalRef,
        *,
        environment: str | None = None,
        limit: int = SAVED_QUERY_LIST_DEFAULT,
        cursor: str | None = None,
    ) -> Page[SavedQueryView]:
        """List the saved queries the caller sees, by name, one page at a time.

        With an `environment`, the saved queries of that environment; the caller needs
        `SUBMIT_QUERY` there unless they own one there or are an administrator. Without one,
        those of every live environment, the caller's `SUBMIT_QUERY` on each asked in one
        `permits_each` batch. Either way the rows are read with one statement whose condition
        holds what the caller sees. The order is the name in any letter case, then the
        environment, then the id; the two lists have their own cursors.

        Parameters
        ----------
        p
            The caller.
        environment
            The environment's name, or `None` for every environment.
        limit
            The page size, 1 to `SAVED_QUERY_LIST_MAX`.
        cursor
            The `next_cursor` of the previous page of the same list, or `None` for the first.

        Returns
        -------
        Page of SavedQueryView
            This page's saved queries and the cursor of the next page, `None` on the last.

        Raises
        ------
        PermissionDenied
            If `p` authenticated with a task token, or, for one environment, lacks
            `SUBMIT_QUERY` there, owns no saved query there and is not an administrator.
        NotFoundError
            If the named environment does not exist.
        ValidationError
            If `limit` is out of range, or `cursor` is malformed or from the other list.
        """
        _refuse_task_token(p)
        conditions: list[ColumnElement[bool]] = [LIVE_ENVIRONMENT]
        if environment is None:
            live = self._session.execute(
                select(Environment.id, Environment.name).where(LIVE_ENVIRONMENT)
            ).all()
            reader = self._reader(p, {env_id: name for env_id, name in live})
            kind = _EVERY_ENVIRONMENT
        else:
            env = self._environment(environment)
            reader = self._reader(p, {env.id: env.name})
            if not (reader.queryable or reader.admin or self._owns_one(p, env)):
                self._authz.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(env.name))
            conditions.append(SavedQuery.environment_id == env.id)
            kind = _ONE_ENVIRONMENT
        rows = self._session.execute(
            select(SavedQuery, Environment.name, Principal.name)
            .join(Environment, Environment.id == SavedQuery.environment_id)
            .outerjoin(Principal, Principal.id == SavedQuery.owner_id)
            .where(*conditions, reader.visible())
        ).all()
        return page_in_order(
            [reader.view(row, env_name, owner) for row, env_name, owner in rows],
            kind=kind,
            key_of=lambda v: (v.name.casefold(), v.environment, str(v.id)),
            limit=limit,
            cursor=cursor,
        )

    def _environment(self, environment: str) -> Environment:
        """Find a live environment by name.

        Parameters
        ----------
        environment
            The name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        NotFoundError
            If there is none, or it is deleted.
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == environment, LIVE_ENVIRONMENT)
        ).scalar_one_or_none()
        if env is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        return env

    def _reader(self, p: PrincipalRef, environments: Mapping[uuid.UUID, str]) -> _Reader:
        """Ask what the caller may see in some environments, `SUBMIT_QUERY` in one batch.

        Parameters
        ----------
        p
            The caller.
        environments
            The environments' names by id.

        Returns
        -------
        _Reader
            The caller's rights. An administrator is one with the `ADMIN` role at instance
            scope who authenticated with a personal token or a session, never a CI token.
        """
        ids = list(environments)
        answers = (
            permits_each(
                self._authz,
                p,
                [
                    (Action.SUBMIT_QUERY, Securable.environment_scope(n))
                    for n in environments.values()
                ],
            )
            if ids
            else []
        )
        return _Reader(
            p,
            admin=p.ci_scope is None and acts_as_instance_admin(self._session, self._authz, p),
            queryable=frozenset(i for i, ok in zip(ids, answers, strict=True) if ok),
            reads_text=lambda: self._authz.permits(
                p, Action.READ_QUERY_TEXT, Securable.instance_scope()
            ),
        )

    def _seen(
        self, p: PrincipalRef, env: Environment, saved_query_id: uuid.UUID
    ) -> tuple[SavedQuery, str | None, _Reader]:
        """Find a saved query the caller sees in a live environment.

        Parameters
        ----------
        p
            The caller.
        env
            The environment.
        saved_query_id
            The saved query's id.

        Returns
        -------
        tuple of (SavedQuery, str or None, _Reader)
            The row, its owner's name (`None` once deleted) and the caller's rights.

        Raises
        ------
        NotFoundError
            If the environment holds no such saved query, or the caller does not see it; the
            same text for both.
        """
        found = self._session.execute(
            select(SavedQuery, Principal.name)
            .outerjoin(Principal, Principal.id == SavedQuery.owner_id)
            .where(SavedQuery.id == saved_query_id, SavedQuery.environment_id == env.id)
        ).one_or_none()
        if found is not None:
            row, owner = found
            reader = self._reader(p, {env.id: env.name})
            if reader.sees(row):
                return row, owner, reader
        raise NotFoundError(f"No saved query {saved_query_id} in {env.name!r}.")

    def _owns_one(self, p: PrincipalRef, env: Environment) -> bool:
        """Say whether the caller owns a saved query in an environment.

        Parameters
        ----------
        p
            The caller.
        env
            The environment.

        Returns
        -------
        bool
            Whether one exists.
        """
        return bool(
            self._session.execute(
                select(
                    exists().where(SavedQuery.environment_id == env.id, SavedQuery.owner_id == p.id)
                )
            ).scalar()
        )

    def _refuse_duplicate(
        self, p: PrincipalRef, env: Environment, name: str, saved_query_id: uuid.UUID | None
    ) -> None:
        """Refuse a name the caller already gave another saved query in the environment.

        Parameters
        ----------
        p
            The caller, the owner.
        env
            The environment.
        name
            The stripped name.
        saved_query_id
            The saved query being changed, which may keep its name; `None` for a new one.

        Raises
        ------
        AlreadyExistsError
            If another of the caller's saved queries there has the name; the message names it.
        """
        conditions: list[Any] = [
            SavedQuery.environment_id == env.id,
            SavedQuery.owner_id == p.id,
            SavedQuery.name == name,
        ]
        if saved_query_id is not None:
            conditions.append(SavedQuery.id != saved_query_id)
        existing = self._session.execute(select(SavedQuery.id).where(*conditions)).scalar()
        if existing is not None:
            raise AlreadyExistsError(
                f"You already have a saved query named {name!r} in {env.name!r} ({existing}); "
                "choose another name, or update that one."
            )

    def _save(
        self,
        p: PrincipalRef,
        env: Environment,
        row: SavedQuery,
        name: str,
        sql: str,
        visibility: SavedQueryVisibility,
        event: AuditEventType,
    ) -> SavedQueryView:
        """Write the fields of one of the caller's saved queries, audit it and commit.

        Parameters
        ----------
        p
            The caller, the owner.
        env
            The environment.
        row
            The saved query, new or loaded.
        name
            The stripped name.
        sql
            The SQL.
        visibility
            The visibility; `private_owner_id` follows it.
        event
            `SAVED_QUERY_CREATED` or `SAVED_QUERY_UPDATED`.

        Returns
        -------
        SavedQueryView
            The saved query, as its owner sees it.

        Raises
        ------
        AlreadyExistsError
            If a concurrent request gave another of the caller's saved queries there the same
            name first.
        IntegrityError
            If the write breaks another constraint, which no checked request does.
        """
        keeps_name = None if event is AuditEventType.SAVED_QUERY_CREATED else row.id
        row.name = name
        row.sql_text = sql
        row.visibility = visibility.value
        row.private_owner_id = p.id if visibility is SavedQueryVisibility.PRIVATE else None
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            self._refuse_duplicate(p, env, name, keeps_name)
            raise
        self._record(p, env, row, p.name, event)
        # The owner's own view: their SQL and, having just been authorized, both actions.
        view = _Reader(
            p, admin=False, queryable=frozenset({env.id}), reads_text=lambda: False
        ).view(row, env.name, p.name)
        self._session.commit()
        return view

    def _record(
        self,
        p: PrincipalRef,
        env: Environment,
        row: SavedQuery,
        owner: str | None,
        event: AuditEventType,
    ) -> None:
        """Add a saved query's audit event to the change's transaction, without its SQL.

        Parameters
        ----------
        p
            The caller.
        env
            The environment, the event's securable.
        row
            The saved query.
        owner
            Its owner's name, or `None` once deleted.
        event
            The event type.
        """
        AuditService(self._session).record(
            p.id,
            event,
            securable=Securable.environment_scope(env.name),
            detail={
                "id": str(row.id),
                "name": row.name,
                "visibility": row.visibility,
                "owner": owner,
            },
        )
