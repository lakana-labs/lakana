"""A repository's glossary rows on SQLite: write, rewrite, delete and the ownership re-check (#375).

The PostgreSQL module (`test_glossary_postgres_integration.py`) proves the same against the
database that ships, with the advisory lock; these run in every coverage job.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.glossary import delete_glossary, write_glossary
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.models import (
    Environment,
    GlossaryTerm,
    GlossaryTermLink,
    GlossaryTermRelated,
    GlossaryTermText,
    Repository,
    RepositoryKind,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.issues import ProjectValidationError

_TERM: dict[str, Any] = {
    "name": "Net revenue",
    "definition": "Revenue after returns.",
    "owner": "finance",
    "synonyms": ["revenue", "sales"],
    "links": {"gold.dim_store": [], "gold.fact_sales": ["net_amount"]},
    "related": ["gross_revenue"],
    "translations": {"fr": {"name": "CA net", "definition": "Après retours.", "synonyms": ["CA"]}},
}
"""A term in its manifest form, with every kind of row."""


def _manifest(terms: dict[str, Any]) -> dict[str, Any]:
    """Build the parts of a version 13 manifest the glossary write reads.

    Parameters
    ----------
    terms
        Id → manifest form.

    Returns
    -------
    dict
        The manifest.
    """
    return {"version": 13, "main_language": "en", "glossary": terms}


@pytest.fixture
def ids(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> tuple[uuid.UUID, Repository, Repository]:
    """Create `dev` and the repositories `shop` and `finance`.

    Parameters
    ----------
    session
        Database session.
    catalog
        Catalog service.
    principal
        The admin.
    tmp_path
        Storage root.

    Returns
    -------
    tuple
        The environment's id, `shop` and `finance`.
    """
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    shop, finance = (
        Repository(id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}")
        for name in ("shop", "finance")
    )
    session.add_all([shop, finance])
    session.commit()
    env = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
    return env, shop, finance


def _counts(session: Session) -> tuple[int, int, int, int]:
    """Count the rows of the four glossary tables.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of int
        Terms, texts, links and related rows.
    """
    models = (GlossaryTerm, GlossaryTermText, GlossaryTermLink, GlossaryTermRelated)
    a, b, c, d = (session.scalar(select(func.count()).select_from(m)) or 0 for m in models)
    return a, b, c, d


def test_a_rewrite_keeps_the_uuid_replaces_the_rows_and_drops_removed_terms(
    session: Session, ids: tuple[uuid.UUID, Repository, Repository]
) -> None:
    env, shop, _ = ids
    margin = {**_TERM, "translations": {"fr": {"name": "Marge"}}}
    write_glossary(session, env, shop.id, _manifest({"net_revenue": _TERM, "margin": margin}))
    session.commit()
    # texts: 2 synonyms each, then fr name, definition and 1 synonym, or the fr name alone
    assert _counts(session) == (2, 8, 4, 2)
    before = session.scalars(
        select(GlossaryTerm.id).where(GlossaryTerm.term_key == "net_revenue")
    ).one()
    write_glossary(session, env, shop.id, _manifest({"net_revenue": {**_TERM, "name": "Net"}}))
    session.commit()
    (row,) = session.scalars(select(GlossaryTerm)).all()
    assert (row.id, row.name, row.main_language) == (before, "Net", "en")
    assert _counts(session) == (1, 5, 2, 1)
    delete_glossary(session, env, shop.id)
    session.commit()
    assert _counts(session) == (0, 0, 0, 0)


def test_recording_a_term_another_repository_owns_is_refused_under_the_lock(
    session: Session, ids: tuple[uuid.UUID, Repository, Repository]
) -> None:
    """The re-check that `record_deploy` and `execute_rollback` run after validation."""
    env, shop, finance = ids
    write_glossary(session, env, finance.id, _manifest({"net_revenue": _TERM}))
    session.commit()
    service = DeployService(session, AllowAllAuthorization())
    with pytest.raises(ProjectValidationError) as caught:
        service._require_own_terms(env, shop.id, _manifest({"net_revenue": _TERM}), "dev")
    assert str(caught.value) == (
        "glossary: net_revenue is a term of repository finance in dev, and a term id belongs to "
        "the repository that deployed it first; rename the term here, or remove it from finance "
        "first."
    )
    assert session.scalars(select(GlossaryTerm.repository_id)).all() == [finance.id]
