"""A ``%`` is refused only in a new table or namespace name; a stored one keeps working.

A storage may read ``%41`` as ``A``, so two new names could share one directory. Names stored
by an older release may hold a ``%``: they are parsed, discovered, matched and addressed as
before, and only a name being created (a table, a view, a namespace, a rename or undrop
target) is refused.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pydantic
import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session
from test_table_location_sweep import _schema
from test_table_writes import _catalog, _ref

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import _table_parts
from lakana_core.identifiers import (
    NamespacePath,
    TableRef,
    refuse_percent_in_new_name,
    validate_path_component,
)
from lakana_core.models import Namespace, Table
from lakana_core.projects.checks import discover_checks
from lakana_core.projects.discovery import discover
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.package_indexes import _validate_name
from lakana_core.projects.project_config import JobName, parse_project_config

_STORED = "a%41b"


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create `dev` at `tmp_path` and return a catalog over it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _catalog(session, principal, tmp_path)


def _stored_table(session: Session, catalog: CatalogService, principal: PrincipalRef) -> TableRef:
    """Create ``gold.a41b`` and rename its row to ``gold.a%41b``, as an older release stored it.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    TableRef
        The stored table.
    """
    catalog.create_table(principal, _ref("gold.a41b"), schema=_schema())
    session.execute(update(Table).where(Table.name == "a41b").values(name=_STORED))
    session.commit()
    return _ref(f"gold.{_STORED}")


@pytest.mark.parametrize("component", ["a%41b", "%", "50%"])
def test_a_new_name_with_a_percent_sign_is_refused_and_says_what_is_allowed(
    component: str,
) -> None:
    """The refusal names the reason and every character that is allowed."""
    with pytest.raises(ValidationError, match="must not contain '%'") as refused:
        refuse_percent_in_new_name(component, what="Table name")
    assert "only letters, decimal digits, '_', '-' and the plain space" in str(refused.value)
    refuse_percent_in_new_name("plain", what="Table name")


@pytest.mark.parametrize(
    "what",
    ["Table name", "Namespace component", "Check name", "Artifact name", "Repository name"],
)
def test_parsing_and_validating_a_name_accept_a_percent_sign(what: str) -> None:
    """The shared rule is about the shape of a reference, not about creating a directory."""
    validate_path_component(_STORED, what=what)
    assert NamespacePath.from_parts([_STORED]).parts == (_STORED,)
    assert TableRef.from_dotted("dev", f"gold.{_STORED}").name == _STORED


def test_a_project_asset_and_check_with_a_percent_sign_are_discovered(tmp_path: Path) -> None:
    """A project that builds a stored ``%`` table can still deploy it."""
    (tmp_path / "assets" / "gold").mkdir(parents=True)
    (tmp_path / "assets" / "gold" / f"{_STORED}.sql").write_text("SELECT 1 AS a\n")
    (tmp_path / "checks" / "gold" / _STORED).mkdir(parents=True)
    (tmp_path / "checks" / "gold" / _STORED / "c%25.sql").write_text(
        f"SELECT 1 FROM gold.{_STORED}\n"
    )

    checkout = discover(tmp_path)
    found, issues = discover_checks(tmp_path, limits=DeployLimits())

    assert [a.table for a in checkout.assets] == [f"gold.{_STORED}"]
    assert issues == []
    assert [(c.table, c.check) for c in found] == [(f"gold.{_STORED}", "c%25")]


def test_a_task_tokens_own_table_with_a_percent_sign_matches_it() -> None:
    """`_table_parts` splits a stored ``%`` name instead of matching nothing."""
    assert _table_parts(f"gold.{_STORED}") == ("gold", _STORED)


def test_job_repository_and_package_index_names_with_a_percent_sign_are_accepted() -> None:
    """These names hold no table directory; the rule never applied to them."""
    assert pydantic.TypeAdapter(JobName).validate_python("nightly%run") == "nightly%run"
    config = parse_project_config('repo: a%b\njobs:\n  nightly%run:\n    select: ["gold.*"]\n')
    assert list(config.jobs) == ["nightly%run"]
    _validate_name("index%1")


def test_a_stored_table_with_a_percent_sign_is_loaded_renamed_away_and_dropped(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The upgrade note's promise: existing names stay usable."""
    ref = _stored_table(session, catalog, principal)

    assert catalog.load_table(principal, ref).ref == ref
    renamed = catalog.rename_table(principal, ref, _ref("gold.ab"))
    assert renamed.ref.name == "ab"
    catalog.drop_table(principal, _ref("gold.ab"))


def test_a_stored_namespace_with_a_percent_sign_takes_a_new_plain_child(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Only the newly named component is checked, not its stored parent."""
    env_id = session.query(Namespace.environment_id).first()
    assert env_id is not None
    session.add(
        Namespace(
            id=uuid.uuid4(),
            environment_id=env_id[0],
            path=NamespacePath.from_parts(["gold", _STORED]).joined(),
            properties={},
        )
    )
    session.commit()

    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["gold", _STORED, "c"]))
    catalog.create_table(principal, _ref(f"gold.{_STORED}.c.t"), schema=_schema())


def test_every_way_of_creating_a_name_refuses_a_percent_sign(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table, an external table, a view, a namespace, a rename target and an undrop target."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    new = _ref("gold.n%41")
    attempts = [
        lambda: catalog.create_table(principal, new, schema=_schema()),
        lambda: catalog.create_external_table(
            principal, _ref("bronze.n%41"), location_pattern="raw/x/*.csv", format="csv"
        ),
        lambda: catalog.create_or_replace_view(
            principal, new, sql="SELECT 1 AS id", references=[], columns=[]
        ),
        lambda: catalog.create_namespace(
            principal, "dev", NamespacePath.from_parts(["gold", "n%41"])
        ),
        lambda: catalog.rename_table(principal, _ref("gold.orders"), new),
        lambda: catalog.undrop_table(principal, _ref("gold.orders"), new_name="n%41"),
    ]
    for attempt in attempts:
        with pytest.raises(ValidationError, match="must not contain '%'"):
            attempt()
    assert catalog.load_table(principal, _ref("gold.orders")).ref.name == "orders"
