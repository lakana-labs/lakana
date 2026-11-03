"""A repository's glossary rows at deploy, rollback and undeploy, on a real PostgreSQL (#375)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.glossary import (
    MAIN_TEXT_LANGUAGE,
    delete_glossary,
    glossary_clashes,
    terms_elsewhere,
    write_glossary,
)
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    Base,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    GlossaryTerm,
    GlossaryTermLink,
    GlossaryTermRelated,
    GlossaryTermText,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
)
from lakana_core.projects.dag import build_graph
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.diff import DeployDiff
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.manifest import build_manifest
from lakana_core.projects.project_config import ResolvedProjectConfig

pytestmark = pytest.mark.integration

_TERM: dict[str, Any] = {
    "name": "Net revenue",
    "definition": "Revenue after returns.",
    "owner": "finance",
    "synonyms": ["revenue", "sales"],
    "links": {"gold.dim_store": [], "gold.fact_sales": ["net_amount"]},
    "related": ["gross_revenue"],
    "translations": {"fr": {"name": "CA net", "synonyms": ["ventes"]}},
}
"""A term in its manifest form, with every kind of row."""

_REFUSED = (
    "glossary: net_revenue is a term of repository finance in dev, and a term id belongs to the "
    "repository that deployed it first; rename the term here, or remove it from finance first."
)
_REFUSED_ROLLBACK = (
    "glossary: net_revenue is a term of repository finance in dev, and a term id belongs to the "
    "repository that deployed it first; roll back to or deploy a version without net_revenue, or "
    "remove it from finance first."
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        yield engine
        engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Give each test a fresh schema.

    Parameters
    ----------
    engine
        The engine.

    Yields
    ------
    Session
        A session on the fresh schema.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@dataclass(frozen=True)
class _World:
    """`dev`, the repositories `shop` and `finance`, a deploy service and its principal.

    Parameters
    ----------
    env
        The environment's id.
    shop
        The repository `shop`.
    finance
        The repository `finance`.
    service
        The deploy service.
    admin
        The deploying principal.
    """

    env: uuid.UUID
    shop: Repository
    finance: Repository
    service: DeployService
    admin: PrincipalRef


@pytest.fixture
def world(session: Session, tmp_path: Path) -> _World:
    """Create `dev`, the repositories `shop` and `finance`, and a principal.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    _World
        What the tests deploy with.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=admin.id, type=admin.type, name="admin", is_active=True))
    shop, finance = (
        Repository(id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}")
        for name in ("shop", "finance")
    )
    session.add_all([shop, finance])
    session.commit()
    env = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
    return _World(env, shop, finance, DeployService(session, AllowAllAuthorization()), admin)


def _manifest(terms: dict[str, Any]) -> dict[str, Any]:
    """Build a version 13 manifest with no assets and the glossary `terms`.

    Parameters
    ----------
    terms
        Id → manifest form.

    Returns
    -------
    dict
        The manifest.
    """
    manifest = build_manifest([], build_graph([]), ResolvedProjectConfig())
    return {**manifest, "version": 13, "main_language": "en", "glossary": terms}


def _claimed(session: Session, run_id: uuid.UUID) -> DeployRun:
    """Mark a queued run as claimed by worker `w1`, the way `run_deploy` does.

    Parameters
    ----------
    session
        Database session; committed here.
    run_id
        The queued run.

    Returns
    -------
    DeployRun
        The run, `running` under `w1`.
    """
    run = session.get(DeployRun, run_id)
    assert run is not None
    run.status = DeployStatus.RUNNING
    run.worker_id = "w1"
    session.commit()
    return run


def _record(w: _World, session: Session, repo: Repository, sha: str, terms: dict) -> JobVersion:
    """Submit and record a deploy of `repo` at `sha` whose glossary is `terms`.

    Parameters
    ----------
    w
        The world.
    session
        Database session.
    repo
        The repository.
    sha
        The commit, one character repeated.
    terms
        Id → manifest form.

    Returns
    -------
    JobVersion
        The recorded version.
    """
    view = w.service.submit(w.admin, "dev", repository=repo.name, kind=DeployKind.DEPLOY)
    run = _claimed(session, view.id)
    run.resolved_sha = sha * 40
    current = w.service.current_job_version(w.env, repo.id)
    return w.service.record_deploy(
        w.admin,
        run,
        manifest=_manifest(terms),
        code_uri="u",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=current.id if current else None,
        worker_id="w1",
    )


def _rollback_run(w: _World, session: Session, repo: Repository, sha: str) -> DeployRun:
    """Submit and claim a rollback of `repo` to the version at `sha`.

    Parameters
    ----------
    w
        The world.
    session
        Database session.
    repo
        The repository.
    sha
        The target's commit, one character repeated.

    Returns
    -------
    DeployRun
        The running rollback.
    """
    view = w.service.submit(
        w.admin, "dev", repository=repo.name, kind=DeployKind.ROLLBACK, to_sha=sha * 40
    )
    return _claimed(session, view.id)


def _owners(session: Session) -> list[tuple[str, str]]:
    """List every term with its owning repository's name.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of tuple
        ``(term id, repository)``, sorted.
    """
    rows = session.execute(
        select(GlossaryTerm.term_key, Repository.name)
        .join(Repository, Repository.id == GlossaryTerm.repository_id)
        .order_by(GlossaryTerm.term_key)
    )
    return [(key, name) for key, name in rows]


def test_a_write_stores_a_term_with_its_texts_links_and_related(
    session: Session, world: _World
) -> None:
    write_glossary(session, world.env, world.shop.id, _manifest({"net_revenue": _TERM}))
    session.commit()
    term = session.scalars(select(GlossaryTerm)).one()
    assert (term.term_key, term.name, term.definition, term.owner) == (
        "net_revenue",
        "Net revenue",
        "Revenue after returns.",
        "finance",
    )
    assert (term.main_language, term.repository_id) == ("en", world.shop.id)
    texts = {
        (t.language, t.field, t.position, t.text) for t in session.scalars(select(GlossaryTermText))
    }
    assert texts == {
        (MAIN_TEXT_LANGUAGE, "synonym", 0, "revenue"),
        (MAIN_TEXT_LANGUAGE, "synonym", 1, "sales"),
        ("fr", "name", 0, "CA net"),
        ("fr", "synonym", 0, "ventes"),
    }
    links = session.scalars(select(GlossaryTermLink).order_by(GlossaryTermLink.position))
    assert [(k.position, k.table_name, k.column_name) for k in links] == [
        (0, "gold.dim_store", None),
        (1, "gold.fact_sales", "net_amount"),
    ]
    related = session.scalars(select(GlossaryTermRelated.related_key))
    assert list(related) == ["gross_revenue"]


def test_a_rewrite_updates_in_place_keeps_the_uuid_and_deletes_removed_terms(
    session: Session, world: _World
) -> None:
    both = {"net_revenue": _TERM, "old": {**_TERM, "related": []}}
    write_glossary(session, world.env, world.shop.id, _manifest(both))
    session.commit()
    kept = session.scalars(
        select(GlossaryTerm.id).where(GlossaryTerm.term_key == "net_revenue")
    ).one()
    renamed = {**_TERM, "name": "Revenue, net", "synonyms": []}
    write_glossary(session, world.env, world.shop.id, _manifest({"net_revenue": renamed}))
    session.commit()
    rows = session.scalars(select(GlossaryTerm)).all()
    assert [(r.id, r.term_key, r.name) for r in rows] == [(kept, "net_revenue", "Revenue, net")]
    assert {t.text for t in session.scalars(select(GlossaryTermText))} == {"CA net", "ventes"}
    assert len(session.scalars(select(GlossaryTermLink)).all()) == 2


def test_a_manifest_without_a_glossary_and_delete_glossary_remove_the_terms(
    session: Session, world: _World
) -> None:
    write_glossary(session, world.env, world.shop.id, _manifest({"net_revenue": _TERM}))
    write_glossary(session, world.env, world.finance.id, _manifest({"margin": _TERM}))
    session.commit()
    write_glossary(session, world.env, world.shop.id, {"version": 12})
    session.commit()
    assert terms_elsewhere(session, world.env, world.shop.id) == {"margin": "finance"}
    delete_glossary(session, world.env, world.finance.id)
    session.commit()
    assert session.scalars(select(GlossaryTerm)).all() == []
    assert session.scalars(select(GlossaryTermText)).all() == []
    assert session.scalars(select(GlossaryTermLink)).all() == []
    assert session.scalars(select(GlossaryTermRelated)).all() == []


def test_a_damaged_entry_is_not_written_and_the_others_are(session: Session, world: _World) -> None:
    damaged = {
        "long_table": {**_TERM, "links": {"a." + "t" * 2000: []}},
        "nul_name": {**_TERM, "name": "Net\x00revenue"},
        "nul_synonym": {**_TERM, "synonyms": ["rev\x00enue"]},
        "nul_table": {**_TERM, "links": {"gold.t\x00": []}},
        "long_related": {**_TERM, "related": ["r" * 64]},
        "many_synonyms": {**_TERM, "synonyms": [f"s{n}" for n in range(21)]},
        "bad_language": {**_TERM, "translations": {"f" * 40: {"name": "x"}}},
    }
    write_glossary(session, world.env, world.shop.id, _manifest({"ok": _TERM, **damaged}))
    session.commit()
    assert [t.term_key for t in session.scalars(select(GlossaryTerm))] == ["ok"]


def test_glossary_clashes_name_the_owner(session: Session, world: _World) -> None:
    write_glossary(session, world.env, world.finance.id, _manifest({"net_revenue": _TERM}))
    session.commit()
    assert glossary_clashes(session, world.env, world.shop.id, ["net_revenue", "other"]) == [
        ("net_revenue", "finance")
    ]
    assert glossary_clashes(session, world.env, world.finance.id, ["net_revenue"]) == []


def test_a_deploy_writes_the_terms_and_an_undeploy_deletes_them(
    session: Session, world: _World
) -> None:
    _record(world, session, world.shop, "a", {"net_revenue": _TERM})
    _record(world, session, world.finance, "b", {"margin": _TERM})
    assert _owners(session) == [("margin", "finance"), ("net_revenue", "shop")]
    world.service.undeploy(world.admin, "dev", "shop")
    assert _owners(session) == [("margin", "finance")]
    assert len(session.scalars(select(GlossaryTermLink)).all()) == 2


def test_a_deploy_of_a_term_another_repository_owns_is_refused(
    session: Session, world: _World
) -> None:
    _record(world, session, world.finance, "a", {"net_revenue": _TERM})
    with pytest.raises(ProjectValidationError) as caught:
        _record(world, session, world.shop, "b", {"net_revenue": _TERM})
    assert str(caught.value) == _REFUSED
    assert _owners(session) == [("net_revenue", "finance")]
    assert world.service.current_job_version(world.env, world.shop.id) is None


def test_a_rollback_writes_the_terms_of_its_target(session: Session, world: _World) -> None:
    _record(world, session, world.shop, "a", {"net_revenue": _TERM})
    _record(world, session, world.shop, "b", {"margin": _TERM})
    assert _owners(session) == [("margin", "shop")]
    run = _rollback_run(world, session, world.shop, "a")
    world.service.execute_rollback(world.admin, run, worker_id="w1")
    assert _owners(session) == [("net_revenue", "shop")]
    assert {t.text for t in session.scalars(select(GlossaryTermText))} == {
        "revenue",
        "sales",
        "CA net",
        "ventes",
    }


def test_a_rollback_to_a_term_another_repository_now_owns_is_refused(
    session: Session, world: _World
) -> None:
    """A rollback re-checks term ownership under the lock and writes nothing when it clashes."""
    _record(world, session, world.shop, "a", {"net_revenue": _TERM})
    current = _record(world, session, world.shop, "b", {})
    _record(world, session, world.finance, "c", {"net_revenue": _TERM})
    run = _rollback_run(world, session, world.shop, "a")
    with pytest.raises(ProjectValidationError) as caught:
        world.service.execute_rollback(world.admin, run, worker_id="w1")
    assert str(caught.value) == _REFUSED_ROLLBACK
    assert _owners(session) == [("net_revenue", "finance")]
    shown = world.service.current_job_version(world.env, world.shop.id)
    assert shown is not None
    assert shown.id == current.id
