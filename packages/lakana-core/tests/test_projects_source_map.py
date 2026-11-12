"""The source map: each deployed asset file, its text and its gate set (`projects.source_map`)."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import sqlite3
import uuid
from collections.abc import Callable
from typing import cast

import pytest
import test_projects_deploys
from sqlalchemy import Delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _python, _repo

import lakana_core.projects.source_map as source_map
from lakana_core.auth import PrincipalRef
from lakana_core.models import (
    JobVersion,
    ProjectFile,
    ProjectFileText,
    Repository,
)
from lakana_core.projects.asset_model import ResolvedReference
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.python_assets import PYTHON_SOURCE_PREFIX
from lakana_core.projects.source_map import (
    GATE_VERSION,
    SOURCE_FILE_MAX_BYTES,
    SOURCE_LINE,
    ProjectFileRecord,
    source_map_records,
    sweep_project_file_texts,
    text_digest,
    write_source_map,
)

ASSET_F = make_asset("gold.f", ("bronze.events",))
"""A SQL asset that reads `bronze.events`, the table the `env` fixture creates."""

REC_F = ProjectFileRecord(
    path="assets/gold/f.sql",
    table="gold.f",
    text="SELECT a FROM bronze.events\n",
    normalized_sql=ASSET_F.normalized_sql,
    gate_tables=("bronze.events", "gold.f"),
    gate_raw=False,
)
"""`ASSET_F`'s record, as the deploy works it out."""

env = test_projects_deploys.env
"""`dev` with `bronze.events` and an empty `silver` namespace, the deploy tests' own fixture."""


def discovered_for(*parsed: ParsedAsset) -> tuple[DiscoveredAsset, ...]:
    """Give the discovered files the parsed assets came from.

    Parameters
    ----------
    *parsed
        The assets.

    Returns
    -------
    tuple of DiscoveredAsset
        One file per asset, its text the body (or a function, for Python).
    """
    return tuple(
        DiscoveredAsset(
            path=a.path,
            table=a.table,
            text=f"{a.sql}\n" if a.sql else "def f():\n    pass\n",
            kind=a.kind,
        )
        for a in parsed
    )


def _rows(session: Session, version_id: uuid.UUID) -> list[str]:
    """List the paths the source map holds for a version.

    Parameters
    ----------
    session
        Database session.
    version_id
        The project version.

    Returns
    -------
    list of str
        The paths, sorted.
    """
    return sorted(
        session.scalars(select(ProjectFile.path).where(ProjectFile.job_version_id == version_id))
    )


def _record(
    session: Session,
    p: PrincipalRef,
    repo: Repository,
    sha: str,
    files: list[ProjectFileRecord],
    *assets: ParsedAsset,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> JobVersion:
    """Deploy as `_deployed` does, with the source map `files`.

    Parameters
    ----------
    session
        Database session.
    p
        The deployer.
    repo
        The repository.
    sha
        The commit.
    files
        The source map's records.
    *assets
        The manifest's assets.
    monkeypatch
        When given, the first lookup of an existing version misses, so the insert meets the
        version already recorded and takes the `IntegrityError` branch; undone before return.

    Returns
    -------
    JobVersion
        The recorded version.
    """
    real_record = cast("Callable[..., JobVersion]", DeployService.record_deploy)
    real_existing = cast("Callable[..., JobVersion | None]", DeployService._existing_job_version)
    seen: list[str] = []

    def with_files(service: DeployService, *args: object, **kwargs: object) -> JobVersion:
        return real_record(service, *args, **kwargs, files=files)

    def blind_first(service: DeployService, *args: object) -> JobVersion | None:
        seen.append("lookup")
        return None if len(seen) == 1 else real_existing(service, *args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(DeployService, "record_deploy", with_files)
        if monkeypatch is not None:
            monkeypatch.setattr(DeployService, "_existing_job_version", blind_first)
        try:
            return _deployed(session, p, repo, sha, *assets)
        finally:
            if monkeypatch is not None:
                monkeypatch.undo()


def test_the_fixed_values_are_the_notes() -> None:
    """The limit, the line and the gate version are the values the security note fixes."""
    assert (SOURCE_FILE_MAX_BYTES, SOURCE_LINE, GATE_VERSION) == (1024 * 1024, 1, 1)
    assert text_digest("é") == hashlib.sha256("é".encode()).hexdigest()
    assert dataclasses.replace(REC_F, text="é").bytes == 2


def test_gate_tables_are_the_union_of_every_name_the_file_uses() -> None:
    """Body references, model reference targets and inputs; a file path sets `gate_raw`."""
    sql = dataclasses.replace(
        make_asset("gold.f", ("silver.a", "x.csv")),
        sql="SELECT * FROM silver.a JOIN 'x.csv' ON true",
    )
    py = _python("gold.p", {"df": "silver.b"})
    [f, p] = source_map_records(
        discovered_for(sql, py),
        [sql, py],
        {"gold.f": (ResolvedReference("k", "gold.dim", "id"),)},
    )
    assert (f.gate_tables, f.gate_raw) == (("gold.dim", "gold.f", "silver.a"), True)
    assert (p.gate_tables, p.gate_raw) == (("gold.p", "silver.b"), False)
    assert (f.path, f.table, f.text, f.normalized_sql) == (
        sql.path,
        "gold.f",
        f"{sql.sql}\n",
        sql.normalized_sql,
    )


def test_a_connection_sets_gate_raw() -> None:
    """A Python asset that uses a connection reads something no table grant covers."""
    py = dataclasses.replace(_python("gold.p", {}), connections=("crm",))
    assert source_map_records(discovered_for(py), [py], {})[0].gate_raw is True


def test_an_asset_without_its_discovered_file_gets_no_record() -> None:
    """The join is by path and table; an asset no discovered file matches is left out."""
    other = dataclasses.replace(discovered_for(ASSET_F)[0], table="gold.other")
    assert source_map_records([other], [ASSET_F], {}) == ()
    assert source_map_records([], [ASSET_F], {}) == ()


def test_a_deploy_records_each_asset_file_and_its_text(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """One row per asset file, keyed by the path's digest, and its text by its own."""
    version = _record(session, principal, _repo(session), "a" * 40, [REC_F], ASSET_F)
    row = session.get(ProjectFile, (version.id, text_digest("assets/gold/f.sql")))
    assert row is not None
    assert row.table_sha256 == text_digest("gold.f")
    assert (row.path, row.sha256, row.gate_tables, row.gate_raw) == (
        REC_F.path,
        REC_F.sha256,
        ["bronze.events", "gold.f"],
        False,
    )
    assert (row.table_name, row.line, row.bytes, row.gate_version) == (
        "gold.f",
        1,
        REC_F.bytes,
        GATE_VERSION,
    )
    text = session.get(ProjectFileText, REC_F.sha256)
    assert text is not None
    assert text.text == REC_F.text


def test_two_files_with_the_same_text_share_one_text_row(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Texts are content-addressed: two files with one text store it once."""
    asset_g = dataclasses.replace(ASSET_F, table="gold.g", path="assets/gold/g.sql")
    rec_g = dataclasses.replace(REC_F, table="gold.g", path="assets/gold/g.sql")
    version = _record(
        session, principal, _repo(session), "a" * 40, [rec_g, REC_F, REC_F], ASSET_F, asset_g
    )
    assert _rows(session, version.id) == ["assets/gold/f.sql", "assets/gold/g.sql"]
    assert session.scalars(select(ProjectFileText.sha256)).all() == [REC_F.sha256]


def test_a_redeploy_of_the_same_commit_records_the_source_map(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A version deployed before the source map existed is filled by deploying it again."""
    repo = _repo(session)
    first = _record(session, principal, repo, "a" * 40, [], ASSET_F)
    assert _rows(session, first.id) == []
    again = _record(session, principal, repo, "a" * 40, [REC_F], ASSET_F)
    assert again.id == first.id
    assert _rows(session, first.id) == ["assets/gold/f.sql"]


def test_the_integrity_error_branch_records_the_source_map(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy that meets the version only at its insert still records the files."""
    repo = _repo(session)
    _record(session, principal, repo, "a" * 40, [], ASSET_F)
    version = _record(session, principal, repo, "a" * 40, [REC_F], ASSET_F, monkeypatch=monkeypatch)
    assert _rows(session, version.id) == ["assets/gold/f.sql"]


def test_a_reused_version_never_takes_a_text_that_differs_from_its_manifest(
    session: Session, env: None, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A file whose code is not the manifest's is skipped, and its path is logged."""
    repo = _repo(session)
    first = _record(session, principal, repo, "a" * 40, [], ASSET_F)
    changed = dataclasses.replace(REC_F, text="SELECT 2 AS a\n", normalized_sql="SELECT 2 AS a")
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.source_map"):
        _record(session, principal, repo, "a" * 40, [changed], ASSET_F)
    assert _rows(session, first.id) == []
    assert session.get(ProjectFileText, changed.sha256) is None
    assert "assets/gold/f.sql" in caplog.text


def test_a_record_for_another_table_or_path_is_skipped(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The table, the path and the code must all equal one manifest entry's."""
    repo = _repo(session)
    moved = dataclasses.replace(REC_F, path="assets/gold/moved.sql")
    renamed = dataclasses.replace(REC_F, table="gold.renamed")
    version = _record(session, principal, repo, "a" * 40, [moved, renamed], ASSET_F)
    assert _rows(session, version.id) == []
    assert session.scalars(select(ProjectFileText.sha256)).all() == []


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"assets": "assets/gold/f.sql"},
        {"assets": ["assets/gold/f.sql", {"table": "gold.f", "path": 1, "normalized_sql": "x"}]},
    ],
)
def test_a_manifest_of_an_unexpected_shape_records_nothing(
    session: Session, manifest: dict
) -> None:
    """The manifest is untrusted JSON: anything but the expected entries matches no record."""
    version = JobVersion(id=uuid.uuid4(), manifest_json=manifest)
    assert write_source_map(session, version, [REC_F]) == 0
    assert session.scalars(select(ProjectFileText.sha256)).all() == []


def test_a_current_row_is_never_replaced_on_reuse(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A row at the current gate version keeps its gate, whatever a later deploy sends."""
    repo = _repo(session)
    first = _record(session, principal, repo, "a" * 40, [REC_F], ASSET_F)
    other = dataclasses.replace(REC_F, gate_tables=("gold.f",), gate_raw=True)
    _record(session, principal, repo, "a" * 40, [other], ASSET_F)
    row = session.get(ProjectFile, (first.id, text_digest(REC_F.path)))
    assert row is not None
    assert (row.gate_raw, row.gate_tables) == (False, ["bronze.events", "gold.f"])


def test_a_stale_row_is_replaced_on_reuse(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """A row of an older gate version is rewritten with today's gate."""
    repo = _repo(session)
    first = _record(session, principal, repo, "a" * 40, [REC_F], ASSET_F)
    session.execute(update(ProjectFile).values(gate_version=0, gate_raw=True))
    session.commit()
    _record(session, principal, repo, "a" * 40, [REC_F], ASSET_F)
    row = session.get(ProjectFile, (first.id, text_digest(REC_F.path)))
    assert row is not None
    assert (row.gate_version, row.gate_raw) == (GATE_VERSION, False)


def test_a_reused_version_whose_digest_no_longer_matches_stays_not_recorded(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """Today's parse differs from the version's manifest: no row, so `not_recorded` later."""
    repo = _repo(session)
    first = _record(session, principal, repo, "a" * 40, [], ASSET_F)
    reparsed = dataclasses.replace(REC_F, normalized_sql=REC_F.normalized_sql + " ")
    assert _record(session, principal, repo, "a" * 40, [reparsed], ASSET_F).id == first.id
    assert _rows(session, first.id) == []


def test_the_sweep_deletes_texts_no_version_references(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """An unreferenced text goes; a referenced one stays."""
    _record(session, principal, _repo(session), "a" * 40, [REC_F], ASSET_F)
    session.add(ProjectFileText(sha256="f" * 64, text="orphan"))
    session.commit()
    assert sweep_project_file_texts(session) == 1
    assert session.get(ProjectFileText, "f" * 64) is None
    assert session.get(ProjectFileText, REC_F.sha256) is not None
    assert sweep_project_file_texts(session) == 0


def test_the_sweep_works_in_batches(session: Session) -> None:
    """Batches run until one comes back empty."""
    session.add_all([ProjectFileText(sha256=f"{i:064x}", text="x") for i in range(3)])
    session.commit()
    assert sweep_project_file_texts(session, batch=2) == 3
    assert session.scalars(select(ProjectFileText.sha256)).all() == []


def test_the_sweep_stops_when_asked(session: Session) -> None:
    """`should_stop` is asked after each batch, and a yes ends the sweep there."""
    session.add_all([ProjectFileText(sha256=f"{i:064x}", text="x") for i in range(3)])
    session.commit()
    assert sweep_project_file_texts(session, batch=1, should_stop=lambda: True) == 1


def test_a_text_referenced_meanwhile_is_kept_and_the_sweep_goes_on(
    session: Session, env: None, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete the foreign key refuses (a race) keeps that text; the other texts still go."""
    _record(session, principal, _repo(session), "a" * 40, [REC_F], ASSET_F)
    session.add_all([ProjectFileText(sha256=c * 64, text="orphan") for c in "ef"])
    session.commit()
    real = source_map._unreferenced
    calls: list[int] = []

    def seen_before_the_reference(session: Session, batch: int, refused: set[str]) -> list[str]:
        calls.append(batch)
        return [REC_F.sha256, "e" * 64] if len(calls) == 1 else real(session, batch, refused)

    monkeypatch.setattr(source_map, "_unreferenced", seen_before_the_reference)
    assert sweep_project_file_texts(session, batch=2) == 2
    assert session.scalars(select(ProjectFileText.sha256)).all() == [REC_F.sha256]


def test_another_database_error_of_the_sweep_is_raised(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the foreign key's refusal keeps a text; any other error leaves the sweep."""
    session.add(ProjectFileText(sha256="e" * 64, text="orphan"))
    session.commit()
    real_execute = session.execute

    def locked(statement: object, *args: object, **kwargs: object) -> object:
        if isinstance(statement, Delete):
            raise OperationalError("DELETE", {}, sqlite3.OperationalError("database is locked"))
        return real_execute(statement, *args, **kwargs)  # ty: ignore[no-matching-overload]

    monkeypatch.setattr(session, "execute", locked)
    with pytest.raises(OperationalError, match="database is locked"):
        sweep_project_file_texts(session)


PY_TEXT = "def p(df):\n    return df\n"
"""A Python asset's source."""

ASSET_P = dataclasses.replace(
    _python("gold.p", {"df": "bronze.events"}),
    normalized_sql=PYTHON_SOURCE_PREFIX + text_digest(PY_TEXT),
)
"""A Python asset whose manifest entry holds the digest of `PY_TEXT`."""

REC_P = ProjectFileRecord(
    path=ASSET_P.path,
    table="gold.p",
    text=PY_TEXT,
    normalized_sql=ASSET_P.normalized_sql,
    gate_tables=("bronze.events", "gold.p"),
    gate_raw=False,
)
"""`ASSET_P`'s record."""


def test_a_python_file_is_recorded_when_its_text_is_the_manifests(
    session: Session, env: None, principal: PrincipalRef
) -> None:
    """The text's digest is the one in the manifest entry, so the file is recorded."""
    version = _record(session, principal, _repo(session), "a" * 40, [REC_P], ASSET_P)
    assert _rows(session, version.id) == ["assets/gold/p.py"]


def test_a_python_record_whose_text_is_not_the_manifests_is_skipped(
    session: Session, env: None, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A Python record's `normalized_sql` matches, but its text is another: skipped, logged."""
    other = dataclasses.replace(REC_P, text="def p(df):\n    return None\n")
    with caplog.at_level(logging.WARNING, logger="lakana_core.projects.source_map"):
        version = _record(session, principal, _repo(session), "a" * 40, [other], ASSET_P)
    assert _rows(session, version.id) == []
    assert session.get(ProjectFileText, other.sha256) is None
    assert "assets/gold/p.py" in caplog.text
