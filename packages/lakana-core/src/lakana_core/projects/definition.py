"""Where a table is defined, and the strict content gate in front of the file's text.

`table_definition` answers where the environment's current deployment defines a table: the
repository, the file's path and line, the deployed commit and a link to the file on its git
host. It needs `READ_TABLE` on the table, nothing more: the location reveals no data.

`table_definition_file` adds the file's text, behind a strict gate. The text is shown only to a
reader who holds `READ_ENVIRONMENT`, may read every table the file uses (`gate_tables`, the
defined table included), and has no row filter or column mask on any of them, for a file that
reads no raw path or connection (`gate_raw`) and no view other than the table it defines. Every
failed condition gives the same `restricted` answer, so the answer never tells which one failed.
A row whose gate was worked out by another extraction (`gate_version`) answers `not_recorded`
before any of these conditions, so every reader of the table gets the same answer for it. A text
that is returned is first recorded as `SOURCE_VIEWED`; if that fails, nothing is returned.

The client names only the table. The file's path is a database value and a display string; it
is never opened, joined to a directory or used as an object key.

This module imports nothing from `lakana_core.runs` or `lakana_core.catalog.service`.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from lakana_core.audit.views import record_source_view
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.models import (
    Environment,
    EnvironmentDeployment,
    JobVersion,
    ProjectFile,
    ProjectFileText,
    Repository,
    RepositoryKind,
    Table,
    TableType,
)
from lakana_core.policy.field_binding import UnreadablePolicyError
from lakana_core.policy.resolver import PolicyResolver
from lakana_core.projects.browse import browse_url
from lakana_core.projects.checks import may_read_every
from lakana_core.projects.source_map import (
    GATE_VERSION,
    SOURCE_FILE_MAX_BYTES,
    SOURCE_LINE,
    text_digest,
)


class DefinitionReason(enum.StrEnum):
    """Why a file's text is not returned."""

    RESTRICTED = "restricted"
    NOT_RECORDED = "not_recorded"
    TOO_LARGE = "too_large"


RESTRICTED_MESSAGE: str = (
    "The file is shown only to people who may read this environment and every table the file "
    "uses, without a row filter or column mask. Open it in your git host if you have access "
    "there, or ask an administrator."
)
"""The one answer for every failed condition of the content gate; it names no table."""

NOT_RECORDED_MESSAGE: str = (
    "Lakana has not recorded this file for the deployed version: it was deployed before Lakana "
    "kept project files, or its code no longer matches what this release records. Deploy a new "
    "commit to record the file."
)
"""The answer when the deployed version has no current record of the file."""

TOO_LARGE_MESSAGE: str = (
    "The file is larger than 1 MiB, so it is not shown here. Open it in your git host."
)
"""The answer for a recorded file above `SOURCE_FILE_MAX_BYTES`."""

REASON_MESSAGES: dict[DefinitionReason, str] = {
    DefinitionReason.RESTRICTED: RESTRICTED_MESSAGE,
    DefinitionReason.NOT_RECORDED: NOT_RECORDED_MESSAGE,
    DefinitionReason.TOO_LARGE: TOO_LARGE_MESSAGE,
}
"""The fixed sentence of each reason; a project version's entries use the same ones."""


@dataclass(frozen=True)
class TableDefinition:
    """Where the environment's current deployment defines a table.

    Parameters
    ----------
    defined
        Whether a current deployment defines the table.
    repository
        The repository's name.
    path
        The file's path in the repository.
    commit
        The deployed commit.
    line
        The line the file defines the table on.
    project_version_id
        The deployed project version.
    deployed_at
        When that version was first recorded in the environment.
    browse_url
        A link to the line on the repository's git host, or `None` when none can be built.
    """

    defined: bool
    repository: str | None
    path: str | None
    commit: str | None
    line: int | None
    project_version_id: uuid.UUID | None
    deployed_at: datetime | None
    browse_url: str | None


NOT_DEFINED = TableDefinition(
    defined=False,
    repository=None,
    path=None,
    commit=None,
    line=None,
    project_version_id=None,
    deployed_at=None,
    browse_url=None,
)
"""The answer for a table no current deployment defines."""


@dataclass(frozen=True)
class TableDefinitionFile:
    """A table's definition with the file's text, or why the text is not returned.

    Parameters
    ----------
    definition
        The location.
    content
        The file's text, or `None`.
    bytes
        The size of the text's UTF-8 encoding, set only with `content`.
    reason
        Why `content` is `None`; `None` with content, and for a table no deployment defines.
    message
        The sentence for `reason`, or `None`.
    """

    definition: TableDefinition
    content: str | None
    bytes: int | None
    reason: DefinitionReason | None
    message: str | None


def _refused(definition: TableDefinition, reason: DefinitionReason) -> TableDefinitionFile:
    """Build the answer without text for a reason.

    Parameters
    ----------
    definition
        The location.
    reason
        Why the text is not returned.

    Returns
    -------
    TableDefinitionFile
        No content, no size, the reason and its fixed message.
    """
    return TableDefinitionFile(definition, None, None, reason, REASON_MESSAGES[reason])


def _live_table(session: Session, ref: TableRef) -> Row[*tuple[Any, ...]] | None:
    """Find a table of a live environment.

    Parameters
    ----------
    session
        Database session.
    ref
        The table.

    Returns
    -------
    Row or None
        The table's id, type and environment id, or `None` when it does not exist.
    """
    # Imported here because `lakana_core.catalog` loads the catalog service at import time,
    # which this module may not do at module level (as in `policy.resolver`).
    from lakana_core.catalog.identity import live_table

    return session.execute(
        live_table(
            select(Table.id, Table.table_type, Environment.id.label("environment_id")),
            ref.environment,
            ref.namespace.joined(),
            ref.name,
        )
    ).one_or_none()


def _definition(
    repository: str,
    kind: RepositoryKind,
    url: str,
    version: uuid.UUID,
    commit: str,
    deployed_at: datetime,
    path: str,
) -> TableDefinition:
    """Build a location.

    Parameters
    ----------
    repository
        The repository's name.
    kind
        Its kind.
    url
        Its stored URL.
    version
        The deployed project version.
    commit
        Its commit.
    deployed_at
        When it was first recorded.
    path
        The file's path.

    Returns
    -------
    TableDefinition
        The location, at `SOURCE_LINE`.
    """
    return TableDefinition(
        defined=True,
        repository=repository,
        path=path,
        commit=commit,
        line=SOURCE_LINE,
        project_version_id=version,
        deployed_at=deployed_at,
        browse_url=browse_url(kind, url, commit=commit, path=path, line=SOURCE_LINE),
    )


def _manifest_path(manifest: object, dotted: str) -> str | None:
    """Find the path of a table's asset in a stored manifest, skipping what is malformed.

    Parameters
    ----------
    manifest
        A version's `manifest_json`; untrusted.
    dotted
        The dotted table name.

    Returns
    -------
    str or None
        The asset's path, or `None` when no well-formed entry names the table.
    """
    assets = manifest.get("assets") if isinstance(manifest, Mapping) else None
    for entry in assets if isinstance(assets, list) else []:
        if isinstance(entry, Mapping) and entry.get("table") == dotted:
            path = entry.get("path")
            return path if isinstance(path, str) else None
    return None


def _locate(
    session: Session, authorization: AuthorizationService, p: PrincipalRef, ref: TableRef
) -> tuple[TableDefinition, Row[*tuple[Any, ...]] | None]:
    """Find where the current deployment defines a table, and its source-map row.

    Parameters
    ----------
    session
        Database session.
    authorization
        Authorization service.
    p
        The reader.
    ref
        The table.

    Returns
    -------
    tuple
        The location and the file's row (path, digest, size, gate set), or `None` for the row
        when the version has none (the location then comes from the manifest).

    Raises
    ------
    PermissionDenied
        Without READ_TABLE on the table.
    NotFoundError
        If the environment is missing or deleted, or the table does not exist.
    """
    authorization.authorize(
        p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
    )
    table = _live_table(session, ref)
    if table is None:
        raise NotFoundError(f"Table '{shown(ref.name)}' not found in {shown(str(ref.namespace))}.")
    dotted = f"{ref.namespace}.{ref.name}"
    # A namespace part from before dots were refused would share its dotted name with another
    # table (`["a.b"].c` and `["a", "b"].c`); a deploy only ever names the second.
    if TableRef.from_dotted(ref.environment, dotted) != ref:
        return NOT_DEFINED, None
    row = session.execute(
        select(
            ProjectFile.path,
            ProjectFile.sha256,
            ProjectFile.bytes,
            ProjectFile.gate_tables,
            ProjectFile.gate_raw,
            ProjectFile.gate_version,
            JobVersion.id,
            JobVersion.commit_sha,
            JobVersion.created_at,
            Repository.name,
            Repository.kind,
            Repository.url,
        )
        .join(
            EnvironmentDeployment,
            EnvironmentDeployment.current_job_version_id == ProjectFile.job_version_id,
        )
        .join(JobVersion, JobVersion.id == ProjectFile.job_version_id)
        .join(Repository, Repository.id == EnvironmentDeployment.repository_id)
        .where(
            EnvironmentDeployment.environment_id == table.environment_id,
            ProjectFile.table_sha256 == text_digest(dotted),
            ProjectFile.table_name == dotted,
        )
        .order_by(Repository.name, ProjectFile.path)
        .limit(1)
    ).one_or_none()
    if row is not None:
        location = _definition(
            row.name, row.kind, row.url, row.id, row.commit_sha, row.created_at, row.path
        )
        return location, row
    versions = session.execute(
        select(
            JobVersion.manifest_json,
            JobVersion.id,
            JobVersion.commit_sha,
            JobVersion.created_at,
            Repository.name,
            Repository.kind,
            Repository.url,
        )
        .join(EnvironmentDeployment, EnvironmentDeployment.current_job_version_id == JobVersion.id)
        .join(Repository, Repository.id == EnvironmentDeployment.repository_id)
        .where(EnvironmentDeployment.environment_id == table.environment_id)
        .order_by(Repository.name)
    ).all()
    for version in versions:
        path = _manifest_path(version.manifest_json, dotted)
        if path is not None:
            location = _definition(
                version.name,
                version.kind,
                version.url,
                version.id,
                version.commit_sha,
                version.created_at,
                path,
            )
            return location, None
    return NOT_DEFINED, None


def table_definition(
    session: Session, authorization: AuthorizationService, p: PrincipalRef, ref: TableRef
) -> TableDefinition:
    """Tell where the environment's current deployment defines a table.

    Parameters
    ----------
    session
        Database session.
    authorization
        Authorization service.
    p
        The reader.
    ref
        The table.

    Returns
    -------
    TableDefinition
        The location, or `NOT_DEFINED`.

    Raises
    ------
    PermissionDenied
        Without READ_TABLE on the table (checked first).
    NotFoundError
        If the environment is missing or deleted, or the table does not exist.
    """
    return _locate(session, authorization, p, ref)[0]


def passes_content_gate(
    session: Session,
    authorization: AuthorizationService,
    p: PrincipalRef,
    environment: str,
    table: object,
    gate_tables: object,
    gate_raw: object,
) -> bool:
    """Apply the strict content rule to one file: the rule of every route that shows a file.

    "Defined in" (`table_definition_file`) asks it with a current source-map row; a project
    version (`DeployService.job_version`) asks it for each asset file and each check. The
    conditions use `permits`, so a refusal writes no audit event per gate table.

    Parameters
    ----------
    session
        Database session.
    authorization
        Authorization service.
    p
        The reader.
    environment
        The environment's name.
    table
        The dotted name of the table the file defines (or checks); untrusted.
    gate_tables
        The table names the file uses; untrusted, so anything but a list of strings refuses.
    gate_raw
        Whether the file reads a raw path or a connection; only `False` passes.

    Returns
    -------
    bool
        Whether the reader holds READ_ENVIRONMENT; the file reads no raw path or connection;
        `table` and every gate name are table names the reader may read; no gate table other
        than `table` is a view; and no row filter or column mask (or an unreadable one) applies
        to the reader on any gate table that exists.
    """
    if not authorization.permits(
        p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
    ):
        return False
    if gate_raw is not False or not isinstance(gate_tables, list) or not isinstance(table, str):
        return False
    if not all(isinstance(name, str) for name in gate_tables):
        return False
    dotted = table
    names = sorted({dotted, *gate_tables})
    try:
        gate = [TableRef.from_dotted(environment, name) for name in names]
    except ValidationError:
        return False
    if not may_read_every(authorization, p, environment, names):
        return False
    resolver = PolicyResolver(session)
    for name, table_ref in zip(names, gate, strict=True):
        live = _live_table(session, table_ref)
        if live is None:
            continue
        if live.table_type is TableType.VIEW and name != dotted:
            return False
        try:
            policy = resolver.resolve(p, table_ref, table_id=live.id)
        except UnreadablePolicyError:
            return False
        if policy.row_filters or policy.column_masks:
            return False
    return True


def table_definition_file(
    session: Session,
    authorization: AuthorizationService,
    p: PrincipalRef,
    ref: TableRef,
    *,
    session_factory: Callable[[], Session],
    window_minutes: int,
) -> TableDefinitionFile:
    """Give a table's definition with the file's text, when the reader passes the content gate.

    The first outcome that applies: a table no deployment defines has no content and no
    reason; a version without a current row for the file (none, or one whose `gate_version`
    is not this release's) is `not_recorded`; a reader or file that fails the content rule
    is `restricted`; a file above `SOURCE_FILE_MAX_BYTES` is `too_large`. Otherwise the text is
    read by its digest, the read is recorded as `SOURCE_VIEWED`, and the text is returned.

    Parameters
    ----------
    session
        Database session.
    authorization
        Authorization service.
    p
        The reader.
    ref
        The table.
    session_factory
        Opens the session the audit row is written on (`sessions_like`).
    window_minutes
        `LAKANA_VIEW_AUDIT__WINDOW_MINUTES`.

    Returns
    -------
    TableDefinitionFile
        The location with the text, or with the reason it is not returned.

    Raises
    ------
    PermissionDenied
        Without READ_TABLE on the table (checked first).
    NotFoundError
        If the environment is missing or deleted, or the table does not exist.
    AuditUnavailableError
        If the `SOURCE_VIEWED` row could not be written; no text is returned.
    """
    definition, row = _locate(session, authorization, p, ref)
    if not definition.defined:
        return TableDefinitionFile(definition, None, None, None, None)
    if row is None or row.gate_version != GATE_VERSION:
        return _refused(definition, DefinitionReason.NOT_RECORDED)
    dotted = f"{ref.namespace}.{ref.name}"
    if not passes_content_gate(
        session, authorization, p, ref.environment, dotted, row.gate_tables, row.gate_raw
    ):
        return _refused(definition, DefinitionReason.RESTRICTED)
    if row.bytes > SOURCE_FILE_MAX_BYTES:
        return _refused(definition, DefinitionReason.TOO_LARGE)
    content = session.scalar(
        select(ProjectFileText.text).where(ProjectFileText.sha256 == row.sha256)
    )
    if content is None:
        return _refused(definition, DefinitionReason.NOT_RECORDED)
    record_source_view(
        session_factory,
        p,
        Securable.table_scope(ref.environment, ref.namespace, ref.name),
        repository=row.name,
        path=row.path,
        commit=row.commit_sha,
        window_minutes=window_minutes,
    )
    return TableDefinitionFile(definition, content, row.bytes, None, None)
