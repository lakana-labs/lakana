"""`describe_tables` gives a list's rows exactly what `describe_table` gives each (#622).

The namespace table list describes its rows in one batch: the rights, the deployed manifests and
definitions, the texts, translations, models, glossary terms and facts are read once per request.
These tests build a namespace with every kind of row and every per-row detail the batch reads
(a model with a reference and a generated check, translations, column texts, a glossary link, a
view, a deployed file, a retention) and compare the batch with the per-row path for an
administrator and for a reader who may read only some of the rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from core_test_helpers import deploy_assets
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_describe_table import Star, _texts

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService, TableDescription
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Environment, Repository, RepositoryKind

_GOLD = NamespacePath.from_parts(["gold"])

_GLOSSARY: dict[str, Any] = {
    "version": 13,
    "main_language": "en",
    "glossary": {
        "store": {
            "name": "Store",
            "definition": "A shop.",
            "owner": "sales",
            "links": {"gold.dim_store": [], "gold.fact_sales": ["store_id"]},
            "translations": {"fr": {"name": "Magasin"}},
        },
    },
}
"""One term, linked to a table and to a column of another."""


@pytest.fixture
def world(star: Star) -> Star:
    """Add a view, translations, a glossary term, a deployed file and a retention to the star.

    Parameters
    ----------
    star
        The star of `gold.dim_store` and `gold.fact_sales`.

    Returns
    -------
    Star
        The same star, with `gold.v` beside its tables.
    """
    admin = star.catalog(AllowAllAuthorization())
    admin.create_or_replace_view(
        star.admin,
        TableRef("dev", _GOLD, "v"),
        sql="SELECT store_id FROM gold.dim_store",
        references=admin.resolve_lineage_sources(star.admin, "dev", ["gold.dim_store"]),
        columns=[ColumnSpec(name="store_id", type="long", nullable=True)],
    )
    _texts(star)
    env = star.session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
    shop = Repository(id=uuid.uuid4(), name="shop", kind=RepositoryKind.HTTPS, url="https://h/s")
    star.session.add(shop)
    star.session.flush()
    write_glossary(star.session, env, shop.id, _GLOSSARY)
    star.session.commit()
    deploy_assets(star.session, "dev", "analytics", {"gold.dim_store": {}})
    admin.set_environment_retention(star.admin, "dev", 30)
    return star


@pytest.fixture
def star(session: Session, tmp_path: Path, principal: PrincipalRef) -> Star:
    """Provide the star of `gold.dim_store` and `gold.fact_sales`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The administrator that sets it up.

    Returns
    -------
    Star
        The star.
    """
    return Star(session, tmp_path, principal)


def _per_row(
    catalog: CatalogService, p: PrincipalRef, refs: list[TableRef], languages: list[str]
) -> list[TableDescription]:
    """Describe each readable row on its own, as the list did before the batch.

    Parameters
    ----------
    catalog
        The catalog.
    p
        The reader.
    refs
        The rows.
    languages
        The reader's languages.

    Returns
    -------
    list of TableDescription
        One per row `p` may read.
    """
    return [
        catalog.describe_table(p, ref, languages=languages, view_sql_window_minutes=60)
        for ref in refs
        if catalog.can_read_table(p, ref)
    ]


@pytest.mark.parametrize("languages", [[], ["fr"]])
@pytest.mark.parametrize("reader", ["administrator", "reader of some rows"])
def test_the_batch_describes_each_row_as_the_per_row_path_does(
    world: Star, reader: str, languages: list[str]
) -> None:
    catalog = (
        world.catalog() if reader != "administrator" else world.catalog(AllowAllAuthorization())
    )
    p = world.admin if reader == "administrator" else world.reader_of("gold.fact_sales", "gold.v")
    refs = world.catalog(AllowAllAuthorization()).list_tables(world.admin, "dev", _GOLD)

    batch = catalog.describe_tables(p, refs, languages=languages, view_sql_window_minutes=60)
    one_by_one = _per_row(catalog, p, refs, languages)

    assert batch == one_by_one
    names = [d.ref.name for d in batch]
    assert names == (
        ["dim_store", "fact_sales", "v"] if reader == "administrator" else ["fact_sales", "v"]
    )


def test_the_batch_covers_every_detail_it_reads(world: Star) -> None:
    """The comparison above is not vacuous: each per-row detail is present in the batch."""
    catalog = world.catalog(AllowAllAuthorization())
    refs = catalog.list_tables(world.admin, "dev", _GOLD)

    by_name = {
        d.ref.name: d
        for d in catalog.describe_tables(
            world.admin, refs, languages=["fr"], view_sql_window_minutes=60
        )
    }

    dim, fact, view = by_name["dim_store"], by_name["fact_sales"], by_name["v"]
    assert dim.defined_by is not None and dim.actions
    assert [t.name for t in dim.terms] == ["Magasin"]
    assert [t.name for t in fact.column_terms["store_id"]] == ["Magasin"]
    assert fact.model is not None and fact.model.references and fact.model.checks
    assert (fact.label, fact.label_language) == ("Ventes", "fr")
    # A table a deployment builds does not expire; the other one does.
    assert (dim.expires_at, fact.expires_at is not None) == (None, True)
    assert view.sql == "SELECT store_id FROM gold.dim_store"


def test_a_row_dropped_after_the_listing_is_left_out(world: Star) -> None:
    catalog = world.catalog(AllowAllAuthorization())
    refs = catalog.list_tables(world.admin, "dev", _GOLD)
    catalog.drop_table(world.admin, TableRef("dev", _GOLD, "fact_sales"))

    described = catalog.describe_tables(world.admin, refs)

    assert [d.ref.name for d in described] == ["dim_store", "v"]


def test_an_empty_list_reads_nothing(world: Star) -> None:
    assert world.catalog(AllowAllAuthorization()).describe_tables(world.admin, []) == []
