"""The source map's sweep on Postgres: it skips a text a running deploy holds (`SKIP LOCKED`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_projects_deploys import _manifest
from test_projects_source_map import ASSET_F, REC_F
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    Base,
    Environment,
    JobVersion,
    ProjectFileText,
    Repository,
    RepositoryKind,
)
from lakana_core.projects.source_map import sweep_project_file_texts, write_source_map

pytestmark = pytest.mark.integration


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


def _version(session: Session, tmp_path: Path) -> JobVersion:
    """Record a project version of `ASSET_F` in `dev`, committed, with no source map yet.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    JobVersion
        The version.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    repo = Repository(id=uuid.uuid4(), name="shop", kind=RepositoryKind.HTTPS, url="https://h/s")
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=session.scalars(select(Environment.id)).one(),
        commit_sha="a" * 40,
        manifest_json=_manifest(ASSET_F),
        code_uri="file:///c",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.commit()
    return version


def test_the_sweep_never_deletes_a_text_a_running_deploy_takes(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    """A text the deploy has upserted is locked, so the sweep skips it instead of waiting."""
    version = _version(session, tmp_path)
    session.add(ProjectFileText(sha256=REC_F.sha256, text=REC_F.text))
    session.commit()
    assert write_source_map(session, version, [REC_F]) == 1
    with Session(engine) as other:
        other.execute(text("SET lock_timeout = '2s'"))
        assert sweep_project_file_texts(other) == 0
        session.commit()
        assert other.get(ProjectFileText, REC_F.sha256) is not None
        with pytest.raises(DBAPIError, match="23503"):
            other.execute(delete(ProjectFileText).where(ProjectFileText.sha256 == REC_F.sha256))
        other.rollback()
    assert session.get(ProjectFileText, REC_F.sha256) is not None
