"""Refusals for what a deployed project owns: only git may drop it or take its name.

A table, view or artifact that a current deployment declares is changed by changing the project
and deploying it. Every route that would drop such an object, or create something under a
declared name, refuses alike with `ConflictError`: the platform API, the CLI and the UI through
it, and the Iceberg REST catalog. The repository and the file are named only to a caller who
may read the object (`Action.READ_TABLE`); anyone else is told the fixed `GIT_OWNED_HIDDEN`
sentence, so a refusal discloses no more than that a project defines the name.

This module imports nothing from `lakana_core.catalog.service`, which calls it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ConflictError
from lakana_core.identifiers import TableRef
from lakana_core.projects.definition import DefiningFile, defining_ref, owned_by_git

GIT_OWNED_DROP = (
    "`{name}` is built by a deployed project. Defined in {repository}/{path}. Remove it there "
    "and deploy; then drop it from Orphans. To build it again from git instead: "
    '`lakana table rebuild {name} --env {environment} --reason "…"`.'
)
"""The refusal of a drop of a table or view a deployment builds, to a caller who may read it."""

GIT_OWNED_ARTIFACT_DROP = (
    "`{name}` is produced by a deployed project. Defined in {repository}/{path}. Remove it "
    "there and deploy; then drop it from Orphans. To free space now, delete single versions: "
    "`lakana artifact versions delete {name} VERSION --env {environment}`."
)
"""The refusal of a drop of an artifact a deployment produces, to a caller who may read it."""

GIT_OWNED_CREATE = (
    "`{name}` is declared by a deployed project. Defined in {repository}/{path}. Choose another "
    "name, or remove it there and deploy first."
)
"""The refusal of a create under a declared name, to a caller who may read that name."""

GIT_OWNED_RENAME = (
    "`{name}` is built by a deployed project. Defined in {repository}/{path}. Rename it there "
    "and deploy."
)
"""The refusal of an Iceberg rename that would move a table a deployment builds away."""

GIT_OWNED_RENAME_HIDDEN = "A deployed project defines this table; rename it in git and deploy."
"""The rename refusal to a caller who may not read the table."""

GIT_OWNED_REPLACE = (
    "`{name}` is built by a deployed project. Defined in {repository}/{path}. Change it there "
    "and deploy."
)
"""The refusal of a person's replacement of a view a deployment builds."""

GIT_OWNED_HIDDEN = "A deployed project defines this table; change it in git."
"""Every table refusal (drop or create) to a caller who may not read the table."""

GIT_OWNED_ARTIFACT_HIDDEN = "A deployed project defines this artifact; change it in git."
"""An artifact's refusal (a drop, or a person's upload that would create it) to a caller who
may not read the artifact."""


def git_owned_refusal(
    authz: AuthorizationService,
    p: PrincipalRef,
    ref: TableRef,
    found: DefiningFile,
    *,
    template: str,
    hidden: str,
) -> ConflictError:
    """Build the refusal for an object a deployed project owns.

    Parameters
    ----------
    authz
        The authorization service, asked whether `p` may read `ref`.
    p
        The caller.
    ref
        The object refused.
    found
        The file that declares it.
    template
        The copy naming the file, filled with ``name``, ``repository``, ``path`` and
        ``environment``.
    hidden
        The copy for a caller without `Action.READ_TABLE` on `ref`; never filled.

    Returns
    -------
    ConflictError
        The error to raise.
    """
    scope = Securable.table_scope(ref.environment, ref.namespace, ref.name)
    if not authz.permits(p, Action.READ_TABLE, scope):
        return ConflictError(hidden)
    return ConflictError(
        template.format(
            name=ref.dotted(),
            repository=found.repository,
            path=found.path,
            environment=ref.environment,
        )
    )


def refuse_owned_drop(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    ref: TableRef,
    environment_id: uuid.UUID,
    kind: str,
    *,
    rename: bool = False,
) -> None:
    """Refuse to remove a row a deployed project owns: the declaration and the row's kind match.

    A table under an artifact asset's name, an external table, or a row the current versions
    no longer declare is not owned and passes. Break-glass changes nothing: it gets past the
    protected-environment guard, not this check.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service, for the copy (`git_owned_refusal`).
    p
        The caller.
    ref
        The row's name.
    environment_id
        Its environment.
    kind
        The row's kind (`Table.kind`): ``TABLE``, ``VIEW``, ``EXTERNAL`` or ``ARTIFACT``.
    rename
        The row would be renamed away rather than dropped (`GIT_OWNED_RENAME`).

    Raises
    ------
    ConflictError
        If a current deployment declares the row (`GIT_OWNED_DROP`,
        `GIT_OWNED_ARTIFACT_DROP`, `GIT_OWNED_RENAME`, or the hidden copy).
    """
    found = defining_ref(session, environment_id, ref)
    if found is None or not owned_by_git(found, kind):
        return
    if kind == "ARTIFACT":
        template, hidden = GIT_OWNED_ARTIFACT_DROP, GIT_OWNED_ARTIFACT_HIDDEN
    elif rename:
        template, hidden = GIT_OWNED_RENAME, GIT_OWNED_RENAME_HIDDEN
    else:
        template, hidden = GIT_OWNED_DROP, GIT_OWNED_HIDDEN
    refusal: ConflictError = git_owned_refusal(
        authz, p, ref, found, template=template, hidden=hidden
    )
    raise refusal


def refuse_declared_name(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    ref: TableRef,
    environment_id: uuid.UUID,
    *,
    hidden: str = GIT_OWNED_HIDDEN,
) -> None:
    """Refuse to create anything under a name a current deployment declares, whatever its kind.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service, for the copy (`git_owned_refusal`).
    p
        The caller.
    ref
        The name to take.
    environment_id
        Its environment.
    hidden
        The copy for a caller who may not read the name: `GIT_OWNED_HIDDEN`, or
        `GIT_OWNED_ARTIFACT_HIDDEN` for an artifact.

    Raises
    ------
    ConflictError
        If a current deployment declares the name (`GIT_OWNED_CREATE` or `hidden`).
    """
    found = defining_ref(session, environment_id, ref)
    if found is not None:
        refusal: ConflictError = git_owned_refusal(
            authz, p, ref, found, template=GIT_OWNED_CREATE, hidden=hidden
        )
        raise refusal


def refuse_owned_replace(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    ref: TableRef,
    environment_id: uuid.UUID,
    kind: str,
) -> None:
    """Refuse a person's replacement of a row a deployed project builds, such as a view's SQL.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service, for the copy (`git_owned_refusal`).
    p
        The caller.
    ref
        The row's name.
    environment_id
        Its environment.
    kind
        The row's kind (`Table.kind`).

    Raises
    ------
    ConflictError
        If a current deployment builds the row (`GIT_OWNED_REPLACE` or `GIT_OWNED_HIDDEN`).
    """
    found = defining_ref(session, environment_id, ref)
    if found is None or not owned_by_git(found, kind):
        return
    refusal: ConflictError = git_owned_refusal(
        authz, p, ref, found, template=GIT_OWNED_REPLACE, hidden=GIT_OWNED_HIDDEN
    )
    raise refusal


def undrop_refusal(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    ref: TableRef,
    environment_id: uuid.UUID,
    *,
    renamed: bool,
) -> ConflictError | None:
    """Tell whether restoring a managed table under `ref` would take a declared name.

    A dropped table can come back under its own name when a deployment declares that name as
    a table or a view: that is the table the project builds (an orphan restored, ruling 6).
    Under a name declared as an artifact it would take what git defines as something else.
    Restored under another name (`--as`), it is a create under that name, refused whatever
    the name is declared as.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service, for the copy (`git_owned_refusal`).
    p
        The caller.
    ref
        The name the table would come back under.
    environment_id
        Its environment.
    renamed
        The table comes back under another name than it had.

    Returns
    -------
    ConflictError or None
        The refusal to raise (`GIT_OWNED_CREATE` or `GIT_OWNED_HIDDEN`), or `None`.
    """
    found = defining_ref(session, environment_id, ref)
    if found is None or (not renamed and owned_by_git(found, "TABLE")):
        return None
    return git_owned_refusal(
        authz, p, ref, found, template=GIT_OWNED_CREATE, hidden=GIT_OWNED_HIDDEN
    )
