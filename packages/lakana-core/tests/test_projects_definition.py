"""Where a table is defined, and the strict content gate in front of the file's text.

`table_definition` gives the location to a reader of the table; `table_definition_file` gives
the text only to a reader of the environment who may read every table the file uses, without a
raw read, a view the file reads or a row filter or column mask on any of them, and writes one
`SOURCE_VIEWED` audit row per window when it does.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sys
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import deploy_files, ensure_table
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.audit.views import sessions_like
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import AuditUnavailableError, NotFoundError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NAMESPACE_SEPARATOR, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ColumnMask,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    ProjectFile,
    ProjectFileText,
    RepositoryKind,
    RowFilter,
    Table,
    TableType,
)
from lakana_core.projects.definition import (
    NOT_DEFINED,
    NOT_RECORDED_MESSAGE,
    RESTRICTED_MESSAGE,
    TOO_LARGE_MESSAGE,
    DefinitionReason,
    TableDefinitionFile,
    table_definition,
    table_definition_file,
)
from lakana_core.projects.source_map import SOURCE_FILE_MAX_BYTES, ProjectFileRecord

F_REF = TableRef.from_dotted("dev", "gold.f")
REC = ProjectFileRecord(
    path="assets/gold/f.sql",
    table="gold.f",
    text="SELECT a FROM silver.a\n",
    normalized_sql="SELECT a FROM silver.a",
    gate_tables=("gold.f", "silver.a"),
    gate_raw=False,
)


def _rec(
    table: str = "gold.f", *gate: str, raw: bool = False, body: str | None = None
) -> ProjectFileRecord:
    """Build a record of `table` whose gate set is the table and `gate`.

    Parameters
    ----------
    table
        The dotted table the file defines.
    *gate
        The other table names the file uses.
    raw
        The record's `gate_raw`.
    body
        The file's text; `REC.text` unless given.

    Returns
    -------
    ProjectFileRecord
        The record, at `assets/<namespace>/<name>.sql`.
    """
    return ProjectFileRecord(
        path=f"assets/{table.replace('.', '/')}.sql",
        table=table,
        text=REC.text if body is None else body,
        normalized_sql=f"SELECT 1 -- {table}",
        gate_tables=tuple(sorted({table, *gate})),
        gate_raw=raw,
    )


def _tables(session: Session, *dotted: str) -> dict[str, uuid.UUID]:
    """Make the tables, in `dev`.

    Parameters
    ----------
    session
        Database session.
    *dotted
        The dotted names.

    Returns
    -------
    dict
        Dotted name to table id.
    """
    ids: dict[str, uuid.UUID] = {}
    for name in dotted:
        ref = TableRef.from_dotted("dev", name)
        ids[name] = ensure_table(session, "dev", ref.namespace.joined(), ref.name)[1]
    return ids


def _world(
    session: Session,
    *records: ProjectFileRecord,
    kind: RepositoryKind = RepositoryKind.HTTPS,
    url: str = "https://github.com/acme/analytics.git",
    gate_version: int | None = None,
) -> None:
    """Make `gold.f` and `silver.a` and deploy `records` (`REC` when none) from `analytics`.

    Parameters
    ----------
    session
        Database session.
    *records
        The deployed files.
    kind
        The repository's kind.
    url
        The repository's URL.
    gate_version
        Given, the rows' `gate_version` (`deploy_files`).
    """
    _tables(session, "gold.f", "silver.a")
    deploy_files(
        session,
        "dev",
        "analytics",
        list(records) or [REC],
        kind=kind,
        url=url,
        gate_version=gate_version,
    )


def _person(
    session: Session, name: str, *tables: str, environment: bool = False, viewer: bool = False
) -> PrincipalRef:
    """Persist a user with some grants in `dev`.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The user's name.
    *tables
        Dotted tables the user gets READ_TABLE on.
    environment
        Grant READ_ENVIRONMENT on `dev`.
    viewer
        Grant the VIEWER role on `dev` (every read in it).

    Returns
    -------
    PrincipalRef
        The user.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    grants = GrantService(session, AllowAllAuthorization())
    if viewer:
        grants.grant(
            ref, principal=name, role=Role.VIEWER, kind=SecurableKind.ENVIRONMENT, environment="dev"
        )
    if environment:
        grants.grant(
            ref,
            principal=name,
            action=Action.READ_ENVIRONMENT,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    for dotted in tables:
        table = TableRef.from_dotted("dev", dotted)
        grants.grant(
            ref,
            principal=name,
            action=Action.READ_TABLE,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace=table.namespace.joined(),
            table=table.name,
        )
    return ref


def _analyst(session: Session, name: str = "analyst") -> PrincipalRef:
    """Persist a viewer of `dev`.

    Parameters
    ----------
    session
        Database session.
    name
        The user's name.

    Returns
    -------
    PrincipalRef
        The user.
    """
    return _person(session, name, viewer=True)


def _policy(
    session: Session, kind: str, subject: PrincipalRef, dotted: str, binding: str = "pending"
) -> None:
    """Store a row filter or column mask of `subject` on a table.

    Parameters
    ----------
    session
        Database session; commits.
    kind
        ``mask`` or ``filter``.
    subject
        The principal it applies to.
    dotted
        The table.
    binding
        The binding state stored.
    """
    table = TableRef.from_dotted("dev", dotted)
    env_id, table_id = ensure_table(session, "dev", table.namespace.joined(), table.name)
    common = {
        "id": uuid.uuid4(),
        "principal_id": subject.id,
        "environment": "dev",
        "namespace": table.namespace.joined(),
        "table": table.name,
        "environment_id": env_id,
        "table_id": table_id,
        "created_by": subject.id,
        "created_at": datetime.now(UTC),
        "binding": binding,
    }
    if kind == "mask":
        session.add(ColumnMask(column="a", expression="'***'", **common))
    else:
        session.add(RowFilter(expression="a = 1", **common))
    session.commit()


def _retype(session: Session, dotted: str, table_type: TableType) -> None:
    """Change a table's type, as a replace by a view would.

    Parameters
    ----------
    session
        Database session; commits.
    dotted
        The table.
    table_type
        Its new type.
    """
    session.execute(
        update(Table)
        .where(Table.id == _tables(session, dotted)[dotted])
        .values(table_type=table_type)
    )
    session.commit()


def _file(
    session: Session, p: PrincipalRef, dotted: str = "gold.f", *, window_minutes: int = 60
) -> TableDefinitionFile:
    """Ask for the file that defines a table of `dev`.

    Parameters
    ----------
    session
        Database session.
    p
        The reader.
    dotted
        The table.
    window_minutes
        The audit window.

    Returns
    -------
    TableDefinitionFile
        The answer.
    """
    return table_definition_file(
        session,
        GrantAuthorization(session),
        p,
        TableRef.from_dotted("dev", dotted),
        session_factory=sessions_like(session),
        window_minutes=window_minutes,
    )


def _hidden(got: TableDefinitionFile) -> bool:
    """Tell whether an answer is the `restricted` refusal.

    Parameters
    ----------
    got
        The answer.

    Returns
    -------
    bool
        Whether it holds no content and the `restricted` reason and message.
    """
    return (got.content, got.bytes, got.reason, got.message) == (
        None,
        None,
        DefinitionReason.RESTRICTED,
        RESTRICTED_MESSAGE,
    )


def _without_definition(got: TableDefinitionFile) -> tuple[object, ...]:
    """Give an answer without its location.

    Parameters
    ----------
    got
        The answer.

    Returns
    -------
    tuple
        `content`, `bytes`, `reason` and `message`.
    """
    return (got.content, got.bytes, got.reason, got.message)


def _source_viewed(session: Session) -> list[AuditEvent]:
    """Read the `SOURCE_VIEWED` rows, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.SOURCE_VIEWED)
            .order_by(AuditEvent.occurred_at)
        )
    )


def _statements_of(session: Session, call: Callable[[], object]) -> list[str]:
    """Run `call` and return the source-map and version statements it sent.

    Parameters
    ----------
    session
        The session whose engine is watched.
    call
        What to run.

    Returns
    -------
    list of str
        The statements that name `project_file…` or `job_versions`, in order.
    """
    seen: list[str] = []
    engine = session.get_bind()

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        if "project_file" in statement or "job_versions" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


_OPENED: list[list[str]] = []
"""The lists of the active `_opens` contexts; the hook appends to the innermost."""
_HOOK = {"installed": False}
"""Whether the audit hook is installed: a hook cannot be removed, so it is installed once."""


def _hook(name: str, args: tuple[object, ...]) -> None:
    """Note every `open` audit event while an `_opens` context is active.

    Parameters
    ----------
    name
        The audit event.
    args
        Its arguments; the first of `open` is the path.
    """
    if name == "open" and _OPENED:
        _OPENED[-1].append(str(args[0]))


@contextlib.contextmanager
def _opens() -> Generator[list[str]]:
    """Collect every file opened inside the context (`builtins.open`, `os.open`, `Path`…).

    Yields
    ------
    list of str
        The opened paths.
    """
    if not _HOOK["installed"]:
        sys.addaudithook(_hook)
        _HOOK["installed"] = True
    seen: list[str] = []
    _OPENED.append(seen)
    try:
        yield seen
    finally:
        _OPENED.remove(seen)


def _opens_sees_an_open(tmp_path: Path) -> bool:
    """Show that `_opens` is live: an open of a scratch file inside it is listed.

    Parameters
    ----------
    tmp_path
        A scratch directory.

    Returns
    -------
    bool
        Whether the scratch file was seen.
    """
    probe = tmp_path / "probe.txt"
    with _opens() as seen:
        probe.write_text("x")
    return str(probe) in seen


# --- who may ask --------------------------------------------------------------------------------


def test_definition_needs_read_table(session: Session) -> None:
    _world(session)
    stranger = _person(session, "stranger")
    with pytest.raises(PermissionDenied):
        table_definition(session, GrantAuthorization(session), stranger, F_REF)


def test_definition_file_needs_read_table(session: Session) -> None:
    _world(session)
    with pytest.raises(PermissionDenied):
        _file(session, _person(session, "stranger"))


# --- the location ---------------------------------------------------------------------------------


def test_definition_gives_the_location_to_a_reader_of_the_table(session: Session) -> None:
    _world(session)
    version = session.scalars(select(JobVersion)).one()
    got = table_definition(
        session, GrantAuthorization(session), _person(session, "t", "gold.f"), F_REF
    )
    assert got == dataclasses.replace(
        NOT_DEFINED,
        defined=True,
        repository="analytics",
        path="assets/gold/f.sql",
        commit="a" * 40,
        line=1,
        project_version_id=version.id,
        deployed_at=version.created_at,
        browse_url=f"https://github.com/acme/analytics/blob/{'a' * 40}/assets/gold/f.sql#L1",
    )


def test_not_defined_is_empty() -> None:
    assert dataclasses.astuple(NOT_DEFINED) == (False, None, None, None, None, None, None, None)


def test_definition_reads_no_manifest_for_a_recorded_version(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    statements = _statements_of(
        session, lambda: table_definition(session, GrantAuthorization(session), analyst, F_REF)
    )
    assert statements
    assert not any("manifest_json" in s for s in statements)


def test_an_older_version_answers_not_recorded_with_its_location(session: Session) -> None:
    # a version deployed before Lakana kept project files: no rows, a manifest with the path
    _world(session)
    session.execute(delete(ProjectFile))
    session.execute(
        update(JobVersion).values(
            manifest_json={
                "assets": [
                    "junk",
                    {"table": "gold.g", "path": "assets/gold/g.sql"},
                    {"table": "gold.f", "path": "assets/gold/f.sql"},
                ]
            }
        )
    )
    session.commit()
    got = _file(session, _analyst(session))
    assert (got.definition.path, got.definition.line, got.definition.commit) == (
        "assets/gold/f.sql",
        1,
        "a" * 40,
    )
    assert got.definition.browse_url is not None
    assert (got.content, got.reason, got.message) == (
        None,
        DefinitionReason.NOT_RECORDED,
        NOT_RECORDED_MESSAGE,
    )


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"assets": "junk"},
        {"assets": [{"table": "gold.f", "path": 7}]},
    ],
)
def test_a_manifest_without_a_usable_entry_is_not_defined(
    session: Session, manifest: object
) -> None:
    _world(session)
    session.execute(delete(ProjectFile))
    session.execute(update(JobVersion).values(manifest_json=manifest))
    session.commit()
    got = _file(session, _analyst(session))
    assert (got.definition, got.content, got.reason, got.message) == (NOT_DEFINED, None, None, None)


def test_a_version_whose_file_was_not_recorded_names_the_next_step(session: Session) -> None:
    # ruling 19: the current version's manifest has gold.f, but its file has no row (a
    # reused version whose normalized SQL no longer matches today's parse)
    _world(session)
    session.execute(delete(ProjectFile))
    session.commit()
    got = _file(session, _analyst(session))
    assert got.reason is DefinitionReason.NOT_RECORDED
    assert got.definition.path == "assets/gold/f.sql"
    assert got.message == NOT_RECORDED_MESSAGE
    assert "Deploy a new commit to record the file" in got.message


def test_definition_shows_the_deployed_commit_after_a_newer_push(session: Session) -> None:
    _world(session)
    current = session.scalars(select(JobVersion)).one()
    # a newer version of the repository, recorded but not deployed here
    deploy_files(session, "dev", "analytics", [_rec(body="SELECT 2\n")], sha="b" * 40)
    session.execute(update(EnvironmentDeployment).values(current_job_version_id=current.id))
    session.commit()
    got = _file(session, _analyst(session))
    assert (got.definition.commit, got.content) == ("a" * 40, REC.text)


def test_definition_follows_a_rollback(session: Session) -> None:
    _world(session)
    first = session.scalars(select(JobVersion)).one()
    deploy_files(session, "dev", "analytics", [_rec(body="SELECT 2\n")], sha="b" * 40)
    analyst = _analyst(session)
    assert _file(session, analyst).definition.commit == "b" * 40
    session.execute(update(EnvironmentDeployment).values(current_job_version_id=first.id))
    session.commit()
    got = _file(session, analyst)
    assert (got.definition.commit, got.definition.deployed_at, got.content) == (
        "a" * 40,
        first.created_at,
        REC.text,
    )


def test_a_row_of_a_version_that_is_not_current_is_never_served(session: Session) -> None:
    _world(session)
    # the next version of the same repository no longer defines gold.f
    deploy_files(session, "dev", "analytics", [_rec("gold.other")], sha="b" * 40)
    # another repository's version that is not current anywhere
    other = deploy_files(session, "dev", "other", [REC], sha="c" * 40)
    session.execute(
        delete(EnvironmentDeployment).where(
            EnvironmentDeployment.repository_id == other.repository_id
        )
    )
    session.commit()
    got = _file(session, _analyst(session))
    assert (got.definition, got.content, got.reason) == (NOT_DEFINED, None, None)


def test_a_table_in_a_three_level_namespace_is_found(session: Session) -> None:
    ensure_table(session, "dev", NAMESPACE_SEPARATOR.join(["lake", "raw"]), "orders")
    deploy_files(session, "dev", "analytics", [_rec("lake.raw.orders")])
    got = _file(session, _analyst(session), "lake.raw.orders")
    assert (got.definition.path, got.content) == ("assets/lake/raw/orders.sql", REC.text)


def test_definition_of_a_dropped_table_is_404(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    session.execute(delete(Table).where(Table.name == "f"))
    session.commit()
    with pytest.raises(NotFoundError, match="Table 'f' not found in gold"):
        _file(session, analyst)


def test_definition_of_a_renamed_table_is_not_defined(session: Session) -> None:
    _world(session)
    session.execute(update(Table).where(Table.name == "f").values(name="g"))
    session.commit()
    got = _file(session, _analyst(session), "gold.g")
    assert got.definition == NOT_DEFINED


def test_definition_after_undeploy_is_not_defined(session: Session) -> None:
    _world(session)
    session.execute(delete(EnvironmentDeployment))
    session.commit()
    analyst = _analyst(session)
    assert table_definition(session, GrantAuthorization(session), analyst, F_REF) == NOT_DEFINED


def test_a_local_repository_location_has_no_link(session: Session) -> None:
    _world(session, kind=RepositoryKind.LOCAL, url="/srv/git/analytics")
    got = _file(session, _analyst(session))
    assert (got.definition.repository, got.definition.browse_url, got.content) == (
        "analytics",
        None,
        REC.text,
    )


# --- the content gate -----------------------------------------------------------------------------


def test_a_table_only_grantee_gets_the_location_without_content(session: Session) -> None:
    _world(session)
    got = _file(session, _person(session, "t", "gold.f", "silver.a"))
    assert got.definition.path == "assets/gold/f.sql" and got.definition.browse_url is not None
    assert (got.content, got.reason, got.message) == (
        None,
        DefinitionReason.RESTRICTED,
        RESTRICTED_MESSAGE,
    )


def test_an_environment_reader_gets_the_content(session: Session) -> None:
    _world(session)
    got = _file(session, _analyst(session))
    assert (got.content, got.bytes, got.reason, got.message) == (REC.text, REC.bytes, None, None)


def test_a_reader_of_the_environment_and_every_gate_table_gets_the_content(
    session: Session,
) -> None:
    _world(session)
    got = _file(session, _person(session, "r", "gold.f", "silver.a", environment=True))
    assert got.content == REC.text


def test_the_content_is_returned_exactly(session: Session) -> None:
    body = "SELECT 'é€日本' AS a -- 👩‍💻\n"
    _world(session, _rec(body=body))
    got = _file(session, _analyst(session))
    assert (got.content, got.bytes) == (body, len(body.encode()))


def test_a_model_reference_to_an_unreadable_table_hides_the_file(session: Session) -> None:
    # the front matter's model reference targets silver.secret, which the reader may not read
    _tables(session, "silver.secret")
    _world(session, _rec("gold.f", "silver.a", "silver.secret"))
    got = _file(session, _person(session, "r", "gold.f", "silver.a", environment=True))
    assert _hidden(got)


def test_a_python_reference_to_an_unreadable_table_hides_the_file(session: Session) -> None:
    # `@asset(references=…)` names silver.secret
    body = "@asset(references={'id': 'silver.secret.id'})\ndef f(): ...\n"
    _world(session, _rec("gold.f", "silver.secret", body=body))
    got = _file(session, _person(session, "r", "gold.f", environment=True))
    assert _hidden(got)


def test_a_python_input_to_an_unreadable_table_hides_the_file(session: Session) -> None:
    # `inputs={'a': 'silver.a'}`; the reader holds no grant on silver.a
    body = "@asset(inputs={'a': 'silver.a'})\ndef f(a): ...\n"
    _world(session, _rec("gold.f", "silver.a", body=body))
    got = _file(session, _person(session, "r", "gold.f", environment=True))
    assert _hidden(got)


def test_a_raw_file_read_hides_the_file(session: Session) -> None:
    _world(session, _rec("gold.f", "silver.a", raw=True))
    assert _hidden(_file(session, _analyst(session)))


@pytest.mark.parametrize("stored", ["gold.f", [1], {"gold.f": True}])
def test_a_gate_set_that_is_not_a_list_of_names_hides_the_file(
    session: Session, stored: object
) -> None:
    _world(session)
    session.execute(update(ProjectFile).values(gate_tables=stored))
    session.commit()
    assert _hidden(_file(session, _analyst(session)))


def test_an_old_gate_version_hides_the_file(session: Session) -> None:
    _world(session, gate_version=0)
    got = _file(session, _analyst(session))
    assert got.reason is DefinitionReason.NOT_RECORDED
    assert (got.content, got.message) == (None, NOT_RECORDED_MESSAGE)


def test_a_stale_row_answers_not_recorded_even_to_a_table_only_reader(session: Session) -> None:
    # ruling 13: a reader who would fail READ_ENVIRONMENT and silver.a still gets not_recorded
    _world(session, gate_version=0)
    got = _file(session, _person(session, "t", "gold.f"))
    assert (got.content, got.reason, got.message) == (
        None,
        DefinitionReason.NOT_RECORDED,
        NOT_RECORDED_MESSAGE,
    )


def test_a_reader_with_a_mask_gets_no_content(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    _policy(session, "mask", analyst, "gold.f")
    assert _hidden(_file(session, analyst))


def test_a_reader_with_a_row_filter_on_an_input_gets_no_content(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    _policy(session, "filter", analyst, "silver.a")
    assert _hidden(_file(session, analyst))


def test_an_unreadable_policy_on_a_gate_table_hides_the_file(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    _policy(session, "mask", analyst, "silver.a", binding="bogus")
    assert _hidden(_file(session, analyst))


def test_a_mask_behind_a_view_the_file_reads_hides_the_file(session: Session) -> None:
    # silver.a is a view: its base tables (and their masks) are not in the gate set
    _world(session)
    _retype(session, "silver.a", TableType.VIEW)
    assert _hidden(_file(session, _analyst(session)))


def test_a_table_replaced_by_a_view_after_the_deploy_hides_the_file(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    assert _file(session, analyst).content == REC.text
    _retype(session, "silver.a", TableType.VIEW)
    assert _hidden(_file(session, analyst))


def test_a_view_assets_own_file_is_shown_without_a_policy_on_its_base_tables(
    session: Session,
) -> None:
    _world(session)
    _retype(session, "gold.f", TableType.VIEW)
    assert _file(session, _analyst(session)).content == REC.text


def test_a_view_assets_own_file_is_hidden_when_a_mask_applies_to_a_base_table(
    session: Session,
) -> None:
    _world(session)
    _retype(session, "gold.f", TableType.VIEW)
    analyst = _analyst(session)
    _policy(session, "mask", analyst, "silver.a")
    assert _hidden(_file(session, analyst))


def test_a_gate_name_that_is_not_a_table_name_hides_the_file(session: Session) -> None:
    _world(session, _rec("gold.f", "nodot"))
    assert _hidden(_file(session, _analyst(session)))


def test_a_gate_table_missing_at_read_time_does_not_hide_the_file(session: Session) -> None:
    # silver.later is not built yet; the viewer's grant covers its name
    _world(session, _rec("gold.f", "silver.a", "silver.later"))
    assert _file(session, _analyst(session)).content == REC.text


def test_a_policy_that_does_not_apply_to_the_reader_does_not_hide_the_file(
    session: Session,
) -> None:
    _world(session)
    _policy(session, "mask", _analyst(session, "someone_else"), "silver.a")
    assert _file(session, _analyst(session)).content == REC.text


def test_restricted_answers_are_identical_for_each_failed_condition(session: Session) -> None:
    _tables(session, "silver.b", "silver.vw")
    _world(
        session,
        _rec("gold.f", "silver.a", "silver.b"),
        _rec("gold.raw", raw=True),
        _rec("gold.view_reader", "silver.vw"),
    )
    _tables(session, "gold.raw", "gold.view_reader")
    _retype(session, "silver.vw", TableType.VIEW)
    table_only = _person(session, "t", "gold.f", "silver.a", "silver.b")
    unreadable_ref = _person(session, "u", "gold.f", "silver.a", environment=True)
    analyst = _analyst(session)
    masked = _analyst(session, "masked")
    _policy(session, "filter", masked, "silver.b")
    asked = [
        (table_only, "gold.f"),
        (unreadable_ref, "gold.f"),
        (analyst, "gold.raw"),
        (masked, "gold.f"),
        (analyst, "gold.view_reader"),
    ]
    answers = {_without_definition(_file(session, p, dotted)) for p, dotted in asked}
    assert answers == {(None, None, DefinitionReason.RESTRICTED, RESTRICTED_MESSAGE)}
    assert _file(session, analyst).content == REC.text


# --- size and the stored text ---------------------------------------------------------------------


def test_a_file_over_the_limit_is_not_returned(session: Session) -> None:
    _world(session)
    session.execute(update(ProjectFile).values(bytes=SOURCE_FILE_MAX_BYTES + 1))
    session.commit()
    analyst = _analyst(session)
    answers: list[TableDefinitionFile] = []
    statements = _statements_of(session, lambda: answers.append(_file(session, analyst)))
    (got,) = answers
    assert (got.content, got.bytes, got.reason, got.message) == (
        None,
        None,
        DefinitionReason.TOO_LARGE,
        TOO_LARGE_MESSAGE,
    )
    assert not any("project_file_texts" in s for s in statements)
    assert _source_viewed(session) == []


def test_a_file_at_the_limit_is_returned(session: Session) -> None:
    _world(session)
    session.execute(update(ProjectFile).values(bytes=SOURCE_FILE_MAX_BYTES))
    session.commit()
    assert _file(session, _analyst(session)).content == REC.text


def test_a_missing_text_answers_not_recorded(session: Session) -> None:
    _world(session)
    session.commit()
    session.execute(text("PRAGMA foreign_keys = OFF"))
    session.execute(delete(ProjectFileText))
    session.commit()
    session.execute(text("PRAGMA foreign_keys = ON"))
    got = _file(session, _analyst(session))
    assert (got.content, got.reason, got.message) == (
        None,
        DefinitionReason.NOT_RECORDED,
        NOT_RECORDED_MESSAGE,
    )
    assert _source_viewed(session) == []


def test_a_forged_path_in_project_files_is_never_opened(session: Session, tmp_path: Path) -> None:
    forged = {"../x": "gold.x1", "/etc/passwd": "gold.x2", "%2e%2e/y": "gold.x3"}
    _tables(session, "silver.a", *forged.values())
    deploy_files(
        session,
        "dev",
        "analytics",
        [dataclasses.replace(_rec(table, "silver.a"), path=path) for path, table in forged.items()],
    )
    analyst = _analyst(session)
    with _opens() as opened:
        for path, table in forged.items():
            got = _file(session, analyst, table)
            assert got.definition.path == path and got.content == REC.text
    assert not [p for p in opened if any(bad in p for bad in ("passwd", "../x", "%2e%2e"))]
    assert _opens_sees_an_open(tmp_path)


# --- the audit ------------------------------------------------------------------------------------


def test_reading_a_file_writes_source_viewed_once_per_window(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    _file(session, analyst)
    _file(session, analyst)
    events = _source_viewed(session)
    assert len(events) == 1
    assert events[0].principal_id == analyst.id
    assert events[0].detail == {
        "repository": "analytics",
        "path": "assets/gold/f.sql",
        "commit": "a" * 40,
    }
    assert (events[0].securable_table, events[0].securable_namespace) == ("f", "gold")
    _file(session, analyst, window_minutes=0)
    assert len(_source_viewed(session)) == 2


def test_a_new_commit_writes_source_viewed_again(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    _file(session, analyst)
    deploy_files(session, "dev", "analytics", [REC], sha="b" * 40)
    _file(session, analyst)
    assert [e.detail["commit"] for e in _source_viewed(session)] == ["a" * 40, "b" * 40]


def test_a_restricted_answer_writes_no_source_viewed(session: Session) -> None:
    _world(session)
    assert _hidden(_file(session, _person(session, "t", "gold.f", "silver.a")))
    assert _source_viewed(session) == []


def test_a_failed_audit_write_returns_no_content(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _world(session)
    analyst = _analyst(session)

    def refuse(*args: object, **kwargs: object) -> None:
        """Fail as a full disk would.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("x", {}, Exception())

    monkeypatch.setattr(AuditService, "record", refuse)
    with pytest.raises(AuditUnavailableError, match="a read of a project file is always recorded"):
        _file(session, analyst)
