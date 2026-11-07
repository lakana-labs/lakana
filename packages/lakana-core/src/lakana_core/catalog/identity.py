"""Resolving a grant's or policy's securable names to the ids that bind it.

Lakana binds every grant, row filter, and column mask to the
`environments`/`tables` rows it targets, rather than to the names an
attacker can move by renaming. These lookups are the one place that
translation happens: plain, unauthorized `SELECT`s, deliberately with no
side effects, so both `GrantService`/`RowFilterService`/`ColumnMaskService`
(to raise `NotFoundError` when a target is missing) and
`GrantAuthorization`/`PolicyResolver` (to make the actual authorization
decision) resolve names to ids exactly the same way.

`namespace_exists` is the odd one out: a namespace has no id binding at all
(`Grant` has no `namespace_id` column — a NAMESPACE-kind grant matches on
`environment_id` plus the literal namespace string, since namespaces cannot
be renamed and so their paths are stable). It exists only so `GrantService`
can validate a NAMESPACE-kind grant's target the same way it already
validates an ENVIRONMENT or TABLE one — by existence, at creation time —
even though there is no id for `GrantAuthorization` to later match on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import LIVE_ENVIRONMENT, Environment, Namespace, Table, TableType


def resolve_environment_id(session: Session, environment: str) -> uuid.UUID | None:
    """Resolve an environment name to its id.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.

    Returns
    -------
    uuid.UUID or None
        The environment's id, or `None` if no environment has that name.
    """
    return session.execute(
        select(Environment.id).where(Environment.name == environment, LIVE_ENVIRONMENT)
    ).scalar_one_or_none()


def resolve_table_id(
    session: Session, environment: str, namespace_joined: str, table: str
) -> uuid.UUID | None:
    """Resolve a table's environment/namespace/name to its id.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    namespace_joined
        The table's namespace, in `NamespacePath`'s unit-separator-joined
        storage form (matching `Namespace.path`).
    table
        The table's name.

    Returns
    -------
    uuid.UUID or None
        The table's id, or `None` if no environment, namespace, or table
        matches that combination.
    """
    return session.execute(
        select(Table.id)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .join(Environment, Namespace.environment_id == Environment.id)
        .where(
            Environment.name == environment,
            LIVE_ENVIRONMENT,
            Namespace.path == namespace_joined,
            Table.name == table,
        )
    ).scalar_one_or_none()


def environment_ids_in(session: Session, names: Iterable[str]) -> dict[str, uuid.UUID]:
    """Resolve many environment names to their ids in one statement.

    The batch form of `resolve_environment_id`, with the same filter: a deleted environment
    is not found, so authorization treats its name as one no environment has.

    Parameters
    ----------
    session
        Database session.
    names
        Environment names.

    Returns
    -------
    dict
        The id of each live environment among `names`; a name no live environment has is absent.
    """
    wanted = set(names)
    if not wanted:
        return {}
    return {
        name: env_id
        for name, env_id in session.execute(
            select(Environment.name, Environment.id).where(
                Environment.name.in_(wanted), LIVE_ENVIRONMENT
            )
        )
    }


def table_ids_in(session: Session, environment: str) -> dict[tuple[str, str], uuid.UUID]:
    """Resolve every table of an environment to its id, in one statement.

    The same joins and conditions as `resolve_table_id`, for every table at once: a caller that
    asks about many tables of one environment pays one statement, not one per table.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.

    Returns
    -------
    dict
        ``(namespace in storage form, table name)`` → the table's id; empty when no
        environment has that name.
    """
    rows = session.execute(
        select(Table.id, Namespace.path, Table.name)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .join(Environment, Namespace.environment_id == Environment.id)
        .where(Environment.name == environment, LIVE_ENVIRONMENT)
    )
    return {(path, name): table_id for table_id, path, name in rows}


def resolve_table_with_note(
    session: Session, environment: str, namespace_joined: str, table: str
) -> tuple[uuid.UUID, KeptSeen] | None:
    """Resolve a table to its id and what its row says about data that failed a check.

    The same statement as `resolve_table_id`, with one more column, so a caller that needs
    both pays for one.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    namespace_joined
        The table's namespace, in `NamespacePath`'s unit-separator-joined storage form.
    table
        The table's name.

    Returns
    -------
    tuple or None
        The table's id and its mark as seen (`KeptSeen`), or `None` if no environment,
        namespace, or table matches.
    """
    found = session.execute(
        select(Table.id, Table.kept_run_id, Table.kept_task_run_id, Table.kept_snapshot_id)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .join(Environment, Namespace.environment_id == Environment.id)
        .where(
            Environment.name == environment,
            LIVE_ENVIRONMENT,
            Namespace.path == namespace_joined,
            Table.name == table,
        )
    ).one_or_none()
    return None if found is None else (found[0], KeptSeen(found[1], found[2], found[3]))


def namespace_exists(session: Session, environment: str, namespace_joined: str) -> bool:
    """Report whether a namespace exists in an environment.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    namespace_joined
        The namespace, in `NamespacePath`'s unit-separator-joined storage
        form (matching `Namespace.path`).

    Returns
    -------
    bool
        True if a namespace with that path exists in that environment.
    """
    return (
        session.execute(
            select(Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(
                Environment.name == environment,
                LIVE_ENVIRONMENT,
                Namespace.path == namespace_joined,
            )
        ).first()
        is not None
    )


def artifact_refusal(dotted: str, operation: str) -> str:
    """Word the refusal of a table operation on an artifact.

    Parameters
    ----------
    dotted
        The artifact's dotted name.
    operation
        What was attempted, as a noun phrase (``maintenance``).

    Returns
    -------
    str
        The message; it says that the object is an artifact and where to look.
    """
    return f"{dotted} is an artifact; {operation} applies to tables. See `lakana artifact --help`."


def refuse_view_as_policy_target(
    session: Session, table_id: uuid.UUID, namespace_joined: str, table: str
) -> None:
    """Refuse a row filter or column mask attached to a view or an artifact.

    Views are expanded into their base tables before policies are resolved,
    so a policy on a view would never apply — a control that silently does
    nothing. Invoker's rights already apply every base table's policies. An
    artifact has no rows or columns to filter or mask, so a policy on
    one would also do nothing.

    Parameters
    ----------
    session
        Database session.
    table_id
        The resolved target's id.
    namespace_joined
        Its namespace, in storage form.
    table
        Its name.

    Raises
    ------
    ValidationError
        If the target is a view or an artifact.
    """
    table_type = session.execute(select(Table.table_type).where(Table.id == table_id)).scalar_one()
    if table_type is TableType.ARTIFACT:
        dotted = f"{NamespacePath.parse(namespace_joined)}.{table}"
        raise ValidationError(
            f"{dotted!r} is an artifact; row filters and column masks cannot be attached to an "
            "artifact. Whoever may read an artifact reads all of it."
        )
    if table_type is TableType.VIEW:
        dotted = f"{NamespacePath.parse(namespace_joined)}.{table}"
        raise ValidationError(
            f"{dotted!r} is a view; row filters and column masks apply to tables. "
            "Attach them to the view's base tables."
        )
