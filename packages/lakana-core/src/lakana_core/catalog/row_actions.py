"""What a row of a catalog list says its reader may do.

Every row the platform API lists (a table, a dropped table, an orphan, an artifact, an artifact
version) carries ``actions``: the words of the actions this reader's rights allow on it, kind by
kind. A table or artifact row also carries ``defined_by``: the deployed project file that owns
it, so that only git may remove it.

The rule the clients follow:

- ``actions`` lists what this reader's rights allow, kind by kind; ``defined_by`` says what git
  owns. Neither says anything else.
- A client offers an action only when its word is in ``actions``.
- It shows Drop disabled, with "Defined in" and the file, when ``defined_by`` is set.
- It shows Rebuild when ``"rebuild"`` is in ``actions``, which needs a current deployment to
  declare the name (of any output) besides the rights.
- The server decides every request again: an offered action can still be refused, by git
  ownership, a protected environment, a missing step-up, or the row's state.

The rights of a whole list are asked in one `permits_each` batch, never one call per row, and,
like every visibility question, they write no audit event and no `AUTHORIZATION_DENIED`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.identifiers import TableRef

TABLE_ACTIONS = ("drop", "rebuild", "maintain", "set_retention")
"""The words a table row's `actions` may hold, in this order."""

DROPPED_ACTIONS = ("undrop",)
"""The words a dropped table's `actions` may hold."""

ORPHAN_ACTIONS = ("drop",)
"""The words an orphan's `actions` may hold."""

ARTIFACT_ACTIONS = ("drop",)
"""The words an artifact's `actions` may hold."""

VERSION_ACTIONS = ("delete",)
"""The words an artifact version's `actions` may hold."""

_DROPPABLE = frozenset({"TABLE", "EXTERNAL", "VIEW"})
"""The kinds `drop` and `rebuild` apply to; an artifact is dropped as an artifact."""

_Question = tuple[Action, Securable]


def _ask(
    authz: AuthorizationService, p: PrincipalRef, per_row: Sequence[Sequence[_Question]]
) -> list[list[bool]]:
    """Answer every row's questions in one batch, each distinct question once.

    Parameters
    ----------
    authz
        The authorization service.
    p
        The reader.
    per_row
        The questions of each row.

    Returns
    -------
    list of list of bool
        Each row's answers, in the order of its questions.
    """
    index: dict[_Question, int] = {}
    for questions in per_row:
        for question in questions:
            index.setdefault(question, len(index))
    answers = permits_each(authz, p, list(index)) if index else []
    return [[answers[index[q]] for q in questions] for questions in per_row]


def _table(ref: TableRef) -> Securable:
    """Name a table as a securable.

    Parameters
    ----------
    ref
        The table.

    Returns
    -------
    Securable
        Its table scope.
    """
    return Securable.table_scope(ref.environment, ref.namespace, ref.name)


def table_actions_each(
    authz: AuthorizationService,
    p: PrincipalRef,
    rows: Sequence[tuple[TableRef, str, uuid.UUID | None, bool]],
) -> list[tuple[str, ...]]:
    """Say which of `TABLE_ACTIONS` the reader's rights allow on each table row.

    - ``drop``: `DROP_TABLE` on the table, for a table, a view or an external table.
    - ``rebuild``: `DROP_TABLE` on the table and `RUN` on the environment, for the same kinds,
      and only when a current deployment declares the name (of any output).
    - ``maintain``: `MAINTAIN_TABLE` on the table, for a managed table.
    - ``set_retention``: for a managed table, its owner, or `WRITE_TABLE` on it (a retention
      that shortens the table's life also needs `DROP_TABLE`; the server checks that).

    Parameters
    ----------
    authz
        The authorization service.
    p
        The reader.
    rows
        ``(ref, kind, owner_id, declared)`` per row: the API kind (``TABLE``, ``EXTERNAL``,
        ``VIEW`` or ``ARTIFACT``), the owner, and whether a current deployment declares it.

    Returns
    -------
    list of tuple of str
        Each row's actions, in `TABLE_ACTIONS` order.
    """
    per_row: list[list[_Question]] = []
    for ref, kind, owner_id, declared in rows:
        scope = _table(ref)
        questions: list[_Question] = []
        if kind in _DROPPABLE:
            questions.append((Action.DROP_TABLE, scope))
            if declared:
                questions.append((Action.RUN, Securable.environment_scope(ref.environment)))
        if kind == "TABLE":
            questions.append((Action.MAINTAIN_TABLE, scope))
            if owner_id != p.id:
                questions.append((Action.WRITE_TABLE, scope))
        per_row.append(questions)
    result: list[tuple[str, ...]] = []
    for (ref, kind, owner_id, declared), questions, answers in zip(
        rows, per_row, _ask(authz, p, per_row), strict=True
    ):
        allowed = dict(zip(questions, answers, strict=True))
        scope = _table(ref)
        may_drop = allowed.get((Action.DROP_TABLE, scope), False)
        may_run = allowed.get((Action.RUN, Securable.environment_scope(ref.environment)), False)
        words = {
            "drop": may_drop,
            "rebuild": kind in _DROPPABLE and declared and may_drop and may_run,
            "maintain": allowed.get((Action.MAINTAIN_TABLE, scope), False),
            "set_retention": kind == "TABLE"
            and (owner_id == p.id or allowed.get((Action.WRITE_TABLE, scope), False)),
        }
        result.append(tuple(word for word in TABLE_ACTIONS if words[word]))
    return result


def dropped_actions_each(
    authz: AuthorizationService, p: PrincipalRef, refs: Sequence[TableRef]
) -> list[tuple[str, ...]]:
    """Say whether the reader's rights allow `undrop` on each dropped table.

    The rule of `CatalogService.undrop_table` under the table's own name: `DROP_TABLE` on the
    namespace and `CREATE_TABLE` on the table.

    Parameters
    ----------
    authz
        The authorization service.
    p
        The reader.
    refs
        The dropped tables, under the names they were dropped with.

    Returns
    -------
    list of tuple of str
        ``("undrop",)`` or ``()`` per table.
    """
    per_row: list[list[_Question]] = [
        [
            (Action.DROP_TABLE, Securable.namespace_scope(ref.environment, ref.namespace)),
            (Action.CREATE_TABLE, _table(ref)),
        ]
        for ref in refs
    ]
    return [DROPPED_ACTIONS if all(answers) else () for answers in _ask(authz, p, per_row)]


def drop_actions_each(
    authz: AuthorizationService, p: PrincipalRef, refs: Sequence[TableRef], word: str
) -> list[tuple[str, ...]]:
    """Say whether the reader's rights allow `DROP_TABLE` on each object, under one word.

    For an orphan that still exists and an artifact (``"drop"``) and a live artifact version
    (``"delete"``); the caller leaves out the rows the action cannot apply to.

    Parameters
    ----------
    authz
        The authorization service.
    p
        The reader.
    refs
        The objects.
    word
        The action's word.

    Returns
    -------
    list of tuple of str
        ``(word,)`` or ``()`` per object.
    """
    per_row = [[(Action.DROP_TABLE, _table(ref))] for ref in refs]
    return [(word,) if answers[0] else () for answers in _ask(authz, p, per_row)]
