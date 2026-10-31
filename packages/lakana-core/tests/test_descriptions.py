"""Descriptions of tables and columns: the project's text, an edit, and which one is shown."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session
from test_projects_dag import make_asset

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import service as service_module
from lakana_core.catalog.descriptions import (
    MAX_DESCRIPTION_LENGTH,
    sync_project_descriptions,
)
from lakana_core.catalog.service import MAX_EDIT_LANGUAGES, CatalogService, TableDescription
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ColumnDescription,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Table,
    TableType,
    TextTranslation,
)
from lakana_core.projects.dag import build_graph
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.manifest import build_manifest
from lakana_core.projects.project_config import ResolvedProjectConfig

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "status", StringType(), required=False),
)


def _manifest(
    table: str = "silver.orders",
    description: str | None = "From the project",
    columns: dict[str, str] | None = None,
) -> dict:
    """Build a manifest with one asset that carries a description and column texts.

    Parameters
    ----------
    table
        The asset's dotted table name.
    description
        Its `description:`.
    columns
        Its `columns:` map; `{"id": "Order id", "status": "Where it stands"}` when `None`.

    Returns
    -------
    dict
        The manifest.
    """
    asset = make_asset(
        table,
        metadata=AssetMetadata(
            materialize="table",
            description=description,
            columns=columns if columns is not None else {"id": "Order id", "status": "Stage"},
        ),
    )
    return build_manifest([asset], build_graph([asset]), ResolvedProjectConfig())


def _person(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def catalog_with_orders(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> CatalogService:
    """Provide an allow-all catalog with `dev` and the table `silver.orders` (id, status).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creator, who becomes the table's owner.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    catalog.create_table(principal, _ORDERS, schema=_SCHEMA)
    return catalog


def _described(catalog: CatalogService, principal: PrincipalRef) -> TableDescription:
    """Describe `silver.orders`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The reader.

    Returns
    -------
    TableDescription
        What the platform API would show.
    """
    return catalog.describe_table(principal, _ORDERS)


def test_a_table_with_no_description_shows_none(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """No project text and no edit: no description, no source."""
    shown = _described(catalog_with_orders, principal)
    assert (shown.description, shown.description_source) == (None, None)
    assert shown.column_descriptions == {}


def test_a_deploy_publishes_the_projects_descriptions(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """The asset header's text is the table's and the columns' description, from the project."""
    sync_project_descriptions(session, _env_id(session), _manifest())
    session.commit()
    shown = _described(catalog_with_orders, principal)
    assert (shown.description, shown.description_source) == ("From the project", "project")
    assert {name: (d.text, d.source) for name, d in shown.column_descriptions.items()} == {
        "id": ("Order id", "project"),
        "status": ("Stage", "project"),
    }


def _env_id(session: Session) -> uuid.UUID:
    """Find the id of the `dev` environment.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.scalars(select(Environment.id).where(Environment.name == "dev")).one()


def test_an_edit_is_shown_instead_of_the_projects_text_and_survives_a_redeploy(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """A deploy writes only the project's text; the edit stays on top."""
    catalog = catalog_with_orders
    sync_project_descriptions(session, _env_id(session), _manifest())
    catalog.set_table_description(principal, _ORDERS, "Edited in the API")
    catalog.set_column_description(principal, _ORDERS, "id", "Edited id")

    sync_project_descriptions(
        session, _env_id(session), _manifest(description="Changed in git", columns={"id": "New"})
    )
    session.commit()

    shown = _described(catalog, principal)
    assert (shown.description, shown.description_source) == ("Edited in the API", "edited")
    assert (shown.column_descriptions["id"].text, shown.column_descriptions["id"].source) == (
        "Edited id",
        "edited",
    )
    row = session.scalars(select(Table)).one()
    assert row.description_project == "Changed in git"
    assert row.description_edited == "Edited in the API"


def test_a_reset_goes_back_to_the_projects_text(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """`None` clears the edit; the project's text shows again, then none when it has none."""
    catalog = catalog_with_orders
    sync_project_descriptions(session, _env_id(session), _manifest())
    catalog.set_table_description(principal, _ORDERS, "Mine")
    catalog.set_column_description(principal, _ORDERS, "status", "Mine too")

    catalog.set_table_description(principal, _ORDERS, None)
    catalog.set_column_description(principal, _ORDERS, "status", None)

    shown = _described(catalog, principal)
    assert (shown.description, shown.description_source) == ("From the project", "project")
    assert shown.column_descriptions["status"].text == "Stage"
    assert shown.column_descriptions["status"].source == "project"


def test_a_reset_without_a_project_text_shows_none(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """An edit, then a reset, on a table the project never described."""
    catalog_with_orders.set_table_description(principal, _ORDERS, "Mine")
    assert _described(catalog_with_orders, principal).description_source == "edited"
    catalog_with_orders.set_table_description(principal, _ORDERS, None)
    shown = _described(catalog_with_orders, principal)
    assert (shown.description, shown.description_source) == (None, None)


def test_a_description_over_the_limit_is_refused_with_the_next_step(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """4,000 characters pass; 4,001 do not, for a table and for a column."""
    catalog = catalog_with_orders
    catalog.set_table_description(principal, _ORDERS, "x" * MAX_DESCRIPTION_LENGTH)
    with pytest.raises(ValidationError, match="4,000") as table_error:
        catalog.set_table_description(principal, _ORDERS, "x" * (MAX_DESCRIPTION_LENGTH + 1))
    assert "shorter" in str(table_error.value)
    with pytest.raises(ValidationError, match="4,000"):
        catalog.set_column_description(principal, _ORDERS, "id", "x" * (MAX_DESCRIPTION_LENGTH + 1))


def test_a_blank_description_is_refused(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """Blank text is not a description; the message points to the reset."""
    with pytest.raises(ValidationError, match="reset"):
        catalog_with_orders.set_table_description(principal, _ORDERS, "   ")


def test_a_column_the_table_does_not_have_cannot_be_described(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """The message names the table's columns."""
    with pytest.raises(NotFoundError, match="id, status"):
        catalog_with_orders.set_column_description(principal, _ORDERS, "nope", "text")


def test_a_description_of_a_column_that_is_gone_is_kept_but_not_shown(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """The row of a dropped column stays; the table does not show it."""
    sync_project_descriptions(
        session, _env_id(session), _manifest(columns={"id": "Order id", "gone": "Old column"})
    )
    session.commit()
    shown = _described(catalog_with_orders, principal)
    assert set(shown.column_descriptions) == {"id"}
    assert {row.column for row in session.scalars(select(ColumnDescription))} == {"id", "gone"}


def test_a_column_removed_from_the_header_loses_its_project_text_only(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """A later deploy without the column's text clears the project text and keeps the edit."""
    sync_project_descriptions(session, _env_id(session), _manifest())
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", "Edited")
    sync_project_descriptions(session, _env_id(session), _manifest(columns={"status": "Stage"}))
    session.commit()
    shown = _described(catalog_with_orders, principal)
    assert shown.column_descriptions["id"].source == "edited"
    assert shown.column_descriptions["status"].text == "Stage"
    session.execute(update(ColumnDescription).values(description_edited=None))
    session.commit()
    assert "id" not in _described(catalog_with_orders, principal).column_descriptions


def test_a_deploy_leaves_a_table_that_does_not_exist_yet_alone(
    session: Session, catalog_with_orders: CatalogService
) -> None:
    """Tables a run has not created yet get their text when they are created."""
    sync_project_descriptions(session, _env_id(session), _manifest(table="silver.later"))
    session.commit()
    assert session.scalars(select(Table)).one().description_project is None


def _deploy(session: Session, principal: PrincipalRef, manifest: dict) -> None:
    """Make `manifest` the current deployment of `dev`, without creating its tables.

    Parameters
    ----------
    session
        Database session.
    principal
        Who deployed.
    manifest
        The manifest.
    """
    repo = Repository(id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/r")
    session.add(repo)
    session.commit()
    env_id = _env_id(session)
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha="a" * 40,
        manifest_json=manifest,
        code_uri="file:///x",
        code_sha256="0" * 64,
        deployed_by=principal.id,
    )
    session.add(version)
    session.commit()
    session.add(
        EnvironmentDeployment(
            environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
        )
    )
    session.commit()


def test_a_table_created_after_the_deploy_gets_the_projects_text(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A run creates the table after the deploy; its first read shows the project's text."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    _deploy(session, principal, _manifest())

    catalog.create_table(principal, _ORDERS, schema=_SCHEMA)

    shown = _described(catalog, principal)
    assert (shown.description, shown.description_source) == ("From the project", "project")
    assert shown.column_descriptions["id"].text == "Order id"


def test_a_created_table_says_its_projects_main_language(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The create's answer carries the `main_language:` the deployed project declares."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    _deploy(session, principal, {**_manifest(), "main_language": "de"})

    loaded = catalog.create_table(principal, _ORDERS, schema=_SCHEMA)

    assert loaded.text_language == "de"


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """List the audit events of one type.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The events, oldest first.
    """
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == kind).order_by(AuditEvent.occurred_at)
        )
    )


def test_every_change_is_audited_with_lengths_and_not_the_text(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """Set, change and reset each write one event naming the table, the column and the lengths."""
    catalog = catalog_with_orders
    catalog.set_table_description(principal, _ORDERS, "secret-looking text")
    catalog.set_table_description(principal, _ORDERS, "abc")
    catalog.set_table_description(principal, _ORDERS, None)
    catalog.set_column_description(principal, _ORDERS, "id", "four")

    table_events = _events(session, AuditEventType.TABLE_DESCRIBED)
    assert [e.detail for e in table_events] == [
        {
            "table": "silver.orders",
            "language": "en",
            "old_length": 0,
            "new_length": 19,
            "reset": False,
        },
        {
            "table": "silver.orders",
            "language": "en",
            "old_length": 19,
            "new_length": 3,
            "reset": False,
        },
        {
            "table": "silver.orders",
            "language": "en",
            "old_length": 3,
            "new_length": 0,
            "reset": True,
        },
    ]
    assert table_events[0].securable_table == "orders"
    column_events = _events(session, AuditEventType.COLUMN_DESCRIBED)
    assert [e.detail for e in column_events] == [
        {
            "table": "silver.orders",
            "column": "id",
            "language": "en",
            "old_length": 0,
            "new_length": 4,
            "reset": False,
        }
    ]
    assert "secret-looking" not in str([e.detail for e in table_events + column_events])


def test_the_owner_may_describe_without_any_grant(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A table's owner edits its description; a column edit also needs to read the table."""
    owner = _person(session, "owner")
    setup = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    setup.create_table(owner, _ORDERS, schema=_SCHEMA)
    catalog = _prepared_catalog(session, tmp_path, ActionAllowList(Action.READ_TABLE))
    catalog.set_table_description(owner, _ORDERS, "Mine")
    catalog.set_column_description(owner, _ORDERS, "id", "Mine")
    assert catalog.describe_table(owner, _ORDERS).description == "Mine"


def test_a_manager_may_describe_and_a_reader_may_not(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Without ownership, the right to drop the table (the manager's right) is needed."""
    owner = _person(session, "owner")
    setup = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    setup.create_table(owner, _ORDERS, schema=_SCHEMA)
    reader = _person(session, "reader")
    manager = _person(session, "manager")

    as_reader = _prepared_catalog(session, tmp_path, ActionAllowList(Action.READ_TABLE))
    with pytest.raises(PermissionDenied):
        as_reader.set_table_description(reader, _ORDERS, "No")
    with pytest.raises(PermissionDenied):
        as_reader.set_column_description(reader, _ORDERS, "id", "No")

    as_manager = _prepared_catalog(
        session, tmp_path, ActionAllowList(Action.READ_TABLE, Action.DROP_TABLE)
    )
    as_manager.set_table_description(manager, _ORDERS, "Yes")
    as_manager.set_column_description(manager, _ORDERS, "id", "Yes")
    assert as_manager.describe_table(manager, _ORDERS).description == "Yes"
    assert _events(session, AuditEventType.TABLE_DESCRIBED)[-1].principal_id == manager.id
    assert len(_events(session, AuditEventType.TABLE_DESCRIBED)) == 1


def test_a_refused_change_writes_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A refusal leaves the description and the audit log of changes alone."""
    owner = _person(session, "owner")
    setup = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    setup.create_table(owner, _ORDERS, schema=_SCHEMA)
    stranger = _person(session, "stranger")
    catalog = _prepared_catalog(session, tmp_path, ActionAllowList(Action.READ_TABLE))
    with pytest.raises(PermissionDenied):
        catalog.set_table_description(stranger, _ORDERS, "No")
    assert session.scalars(select(Table)).one().description_edited is None
    assert _events(session, AuditEventType.TABLE_DESCRIBED) == []


def test_a_table_created_through_the_catalog_records_its_creator_as_owner(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """The creator owns the table, which is what lets them describe it."""
    assert session.scalars(select(Table)).one().owner_id == principal.id


def test_a_missing_table_is_not_told_apart_from_a_refusal(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Someone who may not describe tables is refused whether or not the name exists."""
    _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    stranger = _person(session, "stranger")
    catalog = _prepared_catalog(session, tmp_path, ActionAllowList(Action.READ_TABLE))
    with pytest.raises(PermissionDenied):
        catalog.set_table_description(stranger, _ORDERS, "No")
    manager = _prepared_catalog(
        session, tmp_path, ActionAllowList(Action.READ_TABLE, Action.DROP_TABLE)
    )
    with pytest.raises(NotFoundError):
        manager.set_table_description(stranger, _ORDERS, "No")


def test_a_project_description_is_not_written_onto_an_artifact(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """An artifact has no description: a deploy that names it leaves the row alone."""
    row = session.scalars(select(Table)).one()
    row.table_type = TableType.ARTIFACT
    session.commit()
    sync_project_descriptions(session, _env_id(session), _manifest())
    session.commit()
    assert session.scalars(select(Table)).one().description_project is None
    assert session.scalars(select(ColumnDescription)).all() == []


def test_a_manifest_name_that_is_not_dotted_is_skipped(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """A damaged manifest entry does not stop the others from being published."""
    manifest = _manifest()
    manifest["assets"].insert(0, {"table": "nodots", "metadata": {"description": "lost"}})
    sync_project_descriptions(session, _env_id(session), manifest)
    session.commit()
    assert _described(catalog_with_orders, principal).description == "From the project"


def test_resetting_a_column_that_has_no_description_changes_nothing(
    session: Session, catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """A reset of a column with no row writes no row and audits zero lengths."""
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", None)
    assert session.scalars(select(ColumnDescription)).all() == []
    event = _events(session, AuditEventType.COLUMN_DESCRIBED)[-1]
    assert (event.detail["old_length"], event.detail["new_length"], event.detail["reset"]) == (
        0,
        0,
        True,
    )


def test_a_description_needs_the_right_to_read_the_table_and_writes_nothing_without_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """An owner without READ_TABLE is refused before the edit is stored or audited."""
    owner = _person(session, "owner")
    setup = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    setup.create_table(owner, _ORDERS, schema=_SCHEMA)
    catalog = _prepared_catalog(session, tmp_path, ActionAllowList(Action.DROP_TABLE))
    with pytest.raises(PermissionDenied, match="READ_TABLE"):
        catalog.set_table_description(owner, _ORDERS, "No")
    assert session.scalars(select(Table)).one().description_edited is None
    assert _events(session, AuditEventType.TABLE_DESCRIBED) == []


def test_a_label_edit_in_the_main_language_and_one_in_french(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """A label edit without a language is the main language's; one in `fr` is a translation."""
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Orders")
    catalog_with_orders.set_table_description(
        principal, _ORDERS, label="Commandes", description="Une ligne.", language="fr"
    )
    english = catalog_with_orders.describe_table(principal, _ORDERS)
    french = catalog_with_orders.describe_table(principal, _ORDERS, languages=["fr"])
    assert (english.label, english.label_source, english.label_language) == (
        "Orders",
        "edited",
        "en",
    )
    assert (french.label, french.description, french.label_language) == (
        "Commandes",
        "Une ligne.",
        "fr",
    )
    (event,) = [
        e for e in _events(session, AuditEventType.TABLE_DESCRIBED) if e.detail["language"] == "fr"
    ]
    assert event.detail == {
        "table": "silver.orders",
        "language": "fr",
        "old_length": 0,
        "new_length": 10,
        "label_old_length": 0,
        "label_new_length": 9,
        "reset": False,
    }


def test_an_edit_in_a_new_language_is_shown_and_listed(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """A column label edit in `it` lists the language; its reset removes it again."""
    catalog_with_orders.set_column_description(
        principal, _ORDERS, "status", label="Stato", language="it"
    )
    row = session.scalars(select(Table).where(Table.name == "orders")).one()
    assert row.text_languages == ["it"]
    described = catalog_with_orders.describe_table(principal, _ORDERS, languages=["it"])
    assert (described.column_labels["status"].text, described.column_labels["status"].language) == (
        "Stato",
        "it",
    )
    catalog_with_orders.set_column_description(
        principal, _ORDERS, "status", label=None, language="it"
    )
    assert row.text_languages is None


def test_bad_label_edits_and_empty_edits_are_refused_with_the_next_step(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """Blank, too long and multi-line labels, an edit of nothing and a bad tag are refused."""
    for label, message in (
        (" ", "The label of table silver.orders is blank. Write the label, or reset it"),
        (
            "x" * 101,
            "The label of table silver.orders has 101 characters; the limit is 100. "
            "Write a shorter one.",
        ),
        (
            "a\nb",
            "The label of table silver.orders has more than one line; a label is one line. "
            "Put the longer text in the description.",
        ),
    ):
        with pytest.raises(ValidationError, match=re.escape(message)):
            catalog_with_orders.set_table_description(principal, _ORDERS, label=label)
    with pytest.raises(
        ValidationError,
        match=re.escape("send `description`, `label` or both; null removes an edit"),
    ):
        catalog_with_orders.set_table_description(principal, _ORDERS)
    with pytest.raises(ValidationError, match="is not a language tag Lakana takes"):
        catalog_with_orders.set_table_description(principal, _ORDERS, label="X", language="fr_FR")


def test_an_edit_naming_the_main_language_changes_the_main_text_not_a_translation(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """`language="EN"` on an `en` table is the main-language edit, never a translation row."""
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Orders", language="EN")
    catalog_with_orders.set_column_description(
        principal, _ORDERS, "id", "Order number", label="Number", language="en"
    )
    row = session.scalars(select(Table).where(Table.name == "orders")).one()
    assert (row.label_edited, row.text_languages) == ("Orders", None)
    column = session.scalars(select(ColumnDescription)).one()
    assert (column.label_edited, column.description_edited) == ("Number", "Order number")
    (event,) = _events(session, AuditEventType.COLUMN_DESCRIBED)
    assert event.detail == {
        "table": "silver.orders",
        "column": "id",
        "language": "en",
        "old_length": 0,
        "new_length": 12,
        "label_old_length": 0,
        "label_new_length": 6,
        "reset": False,
    }


def test_a_column_label_reset_keeps_the_description_and_a_full_reset_removes_the_row(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """A column row goes only when none of its four texts is left."""
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", "Number", label="No.")
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", label=None)
    column = session.scalars(select(ColumnDescription)).one()
    assert (column.label_edited, column.description_edited) == (None, "Number")
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", None, label=None)
    assert session.scalars(select(ColumnDescription)).all() == []
    event = _events(session, AuditEventType.COLUMN_DESCRIBED)[-1]
    assert event.detail["reset"] is True


def test_a_translated_edit_of_a_column_the_table_does_not_have_is_refused(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """The column check runs for every language, before anything is stored."""
    with pytest.raises(
        NotFoundError, match=re.escape("has no column 'nope'. Its columns: id, status.")
    ):
        catalog_with_orders.set_column_description(
            principal, _ORDERS, "nope", label="Non", language="fr"
        )
    with pytest.raises(
        ValidationError, match=re.escape("The label of column id of table silver.orders")
    ):
        catalog_with_orders.set_column_description(
            principal, _ORDERS, "id", label=" ", language="fr"
        )
    with pytest.raises(ValidationError, match="send `description`, `label` or both"):
        catalog_with_orders.set_column_description(principal, _ORDERS, "id", language="fr")
    assert session.scalars(select(Table)).one().text_languages is None
    assert _events(session, AuditEventType.COLUMN_DESCRIBED) == []


def test_a_french_column_description_edit_is_shown_in_french(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """A description edit in `fr` shows to a French reader with its language."""
    catalog_with_orders.set_column_description(
        principal, _ORDERS, "status", "Où elle en est", language="fr"
    )
    shown_fr = catalog_with_orders.describe_table(principal, _ORDERS, languages=["fr"])
    text = shown_fr.column_descriptions["status"]
    assert (text.text, text.source, text.language) == ("Où elle en est", "edited", "fr")


def _selects(session: Session) -> list[str]:
    """Record the ORM selects of a session, compiled for PostgreSQL.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of str
        The SQL of each select, in order, filled in as the session runs them.
    """
    seen: list[str] = []

    @event.listens_for(session, "do_orm_execute")
    def _record(state: ORMExecuteState) -> None:
        """Keep one select.

        Parameters
        ----------
        state
            The statement about to run.
        """
        if state.is_select:
            seen.append(str(cast("Any", state.statement).compile(dialect=postgresql.dialect())))

    return seen


def _locked_before_texts(seen: list[str]) -> bool:
    """Say whether the table row was locked before any translation or column text was read.

    Parameters
    ----------
    seen
        The selects, from `_selects`.

    Returns
    -------
    bool
        True when a `SELECT … FROM tables … FOR UPDATE` comes first.
    """
    lock = next(
        (i for i, sql in enumerate(seen) if "FROM tables" in sql and "FOR UPDATE" in sql), None
    )
    texts = next(
        (
            i
            for i, sql in enumerate(seen)
            if "FROM text_translations" in sql or "FROM column_descriptions" in sql
        ),
        None,
    )
    return lock is not None and (texts is None or lock < texts)


def test_an_edit_locks_the_table_row_before_it_reads_any_text(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """Two edits, or an edit and a deploy, of one table run one after the other."""
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Commandes", language="fr")
    seen = _selects(session)
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Ordini", language="it")
    assert _locked_before_texts(seen)
    seen.clear()
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", label="No.")
    lock = next(i for i, sql in enumerate(seen) if "FOR UPDATE" in sql)
    assert "FROM tables" in seen[lock]
    assert any("FROM column_descriptions" in sql for sql in seen[lock:])


def test_a_deploy_locks_a_stored_table_before_it_writes_its_texts(
    catalog_with_orders: CatalogService, session: Session
) -> None:
    """The deploy's sync takes the same lock, so it cannot remove an edit made meanwhile."""
    seen = _selects(session)
    sync_project_descriptions(session, _env_id(session), _manifest())
    assert _locked_before_texts(seen)


def test_a_language_missing_from_the_list_is_repaired_by_the_next_edit(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """A row the list does not name is found again by an edit, which lists it once more."""
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Commandes", language="fr")
    session.execute(update(Table).values(text_languages=None))
    session.commit()
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Ordini", language="it")
    row = session.scalars(select(Table)).one()
    assert row.text_languages == ["fr", "it"]
    session.execute(update(Table).values(text_languages=["it"]))
    session.commit()
    catalog_with_orders.set_table_description(principal, _ORDERS, label=None, language="fr")
    assert row.text_languages == ["it"]
    assert [r.language for r in session.scalars(select(TextTranslation))] == ["it"]


@pytest.mark.parametrize("language", [None, "fr"])
@pytest.mark.parametrize("column", [None, "status"])
def test_an_edit_follows_the_deploys_character_rules(
    catalog_with_orders: CatalogService,
    principal: PrincipalRef,
    session: Session,
    language: str | None,
    column: str | None,
) -> None:
    """Labels and descriptions, in the main language and another, as `checked_label` rules."""
    what = "table silver.orders" if column is None else "column status of table silver.orders"

    def edit(**texts: str | None) -> None:
        if column is None:
            catalog_with_orders.set_table_description(
                principal, _ORDERS, language=language, **texts
            )
        else:
            catalog_with_orders.set_column_description(
                principal, _ORDERS, column, language=language, **texts
            )

    for bad in ("a\rb", "a\u2028b", "a\u2029b", "x\u202eevil", "bell\x07"):
        # A description's lone `\r` is a line break (see the Windows line endings test).
        for field in ("label",) if bad == "a\rb" else ("label", "description"):
            with pytest.raises(
                ValidationError,
                match=re.escape(f"The {field} of {what} must not contain a surrogate"),
            ):
                edit(**{field: bad})
    edit(label="  Orders  ", description="  Line one.\n\tLine two.  ")
    shown_texts = catalog_with_orders.describe_table(
        principal, _ORDERS, languages=[language or "en"]
    )
    if column is None:
        label, description = shown_texts.label, shown_texts.description
    else:
        label = shown_texts.column_labels[column].text
        description = shown_texts.column_descriptions[column].text
    assert (label, description) == ("Orders", "Line one.\n\tLine two.")


def test_a_label_of_spaces_around_is_measured_without_them(
    catalog_with_orders: CatalogService, principal: PrincipalRef
) -> None:
    """The limit counts the label as stored, without the surrounding spaces."""
    catalog_with_orders.set_table_description(principal, _ORDERS, label=" " + "x" * 100 + " ")
    assert catalog_with_orders.describe_table(principal, _ORDERS).label == "x" * 100


def test_a_table_has_texts_in_at_most_50_languages_besides_its_main_one(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """A 51st language is refused with the limit and how to free one; listed ones stay editable."""
    row = session.scalars(select(Table)).one()
    tags = [f"a{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(MAX_EDIT_LANGUAGES)]
    for tag in tags:
        session.add(
            TextTranslation(
                id=uuid.uuid4(),
                table_id=row.id,
                subject="table",
                name="",
                language=tag,
                field="label",
                text_project="x",
            )
        )
    row.text_languages = sorted(tags)
    session.commit()
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "Table silver.orders has texts in 50 languages besides its main language, the most "
            "a table may have. To add `fr`, first remove another language's texts: reset its "
            "edits (`lakana table describe silver.orders --lang TAG --reset`, also with "
            "`--column` for each column that has one) and remove its file from translations/."
        ),
    ):
        catalog_with_orders.set_column_description(
            principal, _ORDERS, "id", label="Numéro", language="fr"
        )
    catalog_with_orders.set_table_description(principal, _ORDERS, label="y", language=tags[0])
    catalog_with_orders.set_table_description(principal, _ORDERS, label="Orders")
    catalog_with_orders.set_table_description(principal, _ORDERS, label=None, language="fr")
    assert len(row.text_languages or []) == MAX_EDIT_LANGUAGES


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
@pytest.mark.parametrize("language", [None, "fr"])
def test_a_description_with_windows_line_endings_is_stored_with_newlines(
    catalog_with_orders: CatalogService, principal: PrincipalRef, ending: str, language: str | None
) -> None:
    """Windows and old Mac line endings in a description become newlines; a label stays one line."""
    text = f"Line one.{ending}Line two."
    catalog_with_orders.set_table_description(principal, _ORDERS, text, language=language)
    catalog_with_orders.set_column_description(principal, _ORDERS, "id", text, language=language)
    shown_texts = catalog_with_orders.describe_table(
        principal, _ORDERS, languages=[language or "en"]
    )
    assert shown_texts.description == "Line one.\nLine two."
    assert shown_texts.column_descriptions["id"].text == "Line one.\nLine two."
    with pytest.raises(ValidationError, match=re.escape("has more than one line")):
        catalog_with_orders.set_table_description(
            principal, _ORDERS, label="Line one.\r\nLine two.", language=language
        )


def test_a_main_language_table_edit_audits_the_lengths_it_found_under_the_lock(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """An edit committed between loading the row and locking it is the one the audit replaces."""
    real_lock = service_module.lock_table

    def _edited_meanwhile(locking: Session, row: Table) -> None:
        """Commit another writer's edit just before the lock, behind the loaded row's back.

        Parameters
        ----------
        locking
            The session that locks.
        row
            The table.
        """
        locking.execute(
            update(Table)
            .values(description_edited="Set meanwhile", label_edited="Meanwhile")
            .execution_options(synchronize_session=False)
        )
        real_lock(locking, row)

    with mock.patch.object(service_module, "lock_table", _edited_meanwhile):
        catalog_with_orders.set_table_description(principal, _ORDERS, description="New", label="L")
    event = _events(session, AuditEventType.TABLE_DESCRIBED)[-1]
    assert (event.detail["old_length"], event.detail["label_old_length"]) == (13, 9)


def test_an_edit_decides_its_main_language_under_the_lock(
    catalog_with_orders: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    """A main language changed before the lock turns an `en` edit into a translation."""
    real_lock = service_module.lock_table

    def _deployed_meanwhile(locking: Session, row: Table) -> None:
        """Commit another main language just before the lock, behind the loaded row's back.

        Parameters
        ----------
        locking
            The session that locks.
        row
            The table.
        """
        locking.execute(
            update(Table).values(text_language="fr").execution_options(synchronize_session=False)
        )
        real_lock(locking, row)

    with mock.patch.object(service_module, "lock_table", _deployed_meanwhile):
        catalog_with_orders.set_table_description(principal, _ORDERS, label="Orders", language="en")
    assert session.scalars(select(Table)).one().label_edited is None
    (row,) = session.scalars(select(TextTranslation)).all()
    assert (row.language, row.field, row.text_edited) == ("en", "label", "Orders")
