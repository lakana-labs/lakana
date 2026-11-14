"""Each bound core uses in several places is defined once (#71, item 7)."""

from __future__ import annotations

from sqlalchemy import String

from lakana_core import bounds
from lakana_core.artifacts import manifest as artifact_manifest
from lakana_core.artifacts import store, sweep
from lakana_core.catalog import writes
from lakana_core.models.namespace import Namespace
from lakana_core.models.table import Table
from lakana_core.projects import asset_model
from lakana_core.projects import manifest as project_manifest


def test_the_largest_version_is_one_constant() -> None:
    assert bounds.MAX_INT32 == 2**31 - 1
    assert store.MAX_VERSION is bounds.MAX_INT32
    assert writes.MAX_SOURCE_VERSION is bounds.MAX_INT32
    assert asset_model.MAX_KEEP_VERSIONS is bounds.MAX_INT32


def test_the_sha256_pattern_is_one_constant() -> None:
    assert artifact_manifest.SHA256_PATTERN is bounds.SHA256_HEX
    assert project_manifest.SHA256_HEX is bounds.SHA256_HEX


def test_the_source_name_bound_follows_the_columns() -> None:
    namespace_path = Namespace.__table__.c.path.type
    table_name = Table.__table__.c.name.type
    assert isinstance(namespace_path, String) and isinstance(table_name, String)
    assert (namespace_path.length, table_name.length) == (1024, 255)
    assert writes.MAX_SOURCE_NAME == 1024 + 1 + 255


def test_the_sweep_checks_listed_files_in_the_stores_chunks() -> None:
    assert sweep.FILE_BATCH is store.IN_CHUNK
