"""A rebuild of a table whose stored name today's rules refuse builds it again (#609).

A rebuild drops the table and queues its asset's run, which creates the table as a new one.
The name an older release stored (``price$``) is not a new name while the catalog holds the
dropped table, so the run may create it again.
"""

from __future__ import annotations

from check_run_helpers import _drain
from sqlalchemy import update
from task_run_helpers import World, _entry, _exists, _start, _task

from lakana_core.auth import AllowAllAuthorization
from lakana_core.catalog.rebuild import TableRebuild
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import TableRef
from lakana_core.models import Table, TaskStatus
from lakana_core.runs.service import RunService
from lakana_core.storage.resolver import StorageResolver


def test_a_rebuild_of_a_stored_symbol_name_runs_and_the_table_exists_again(world: World) -> None:
    """Rebuild, run, and `silver.price$` is a table again; nothing was refused."""
    world.set_manifest(_entry("silver.pricex", "SELECT 1::BIGINT AS id"))
    _start(world)
    _drain(world)
    with world.session() as session:
        session.execute(update(Table).where(Table.name == "pricex").values(name="price$"))
        session.commit()
    world.set_manifest(
        _entry(
            "silver.price$",
            "SELECT 2::BIGINT AS id",
            kind="sql",
            path="assets/silver/price$.sql",
            materialize="table",
        )
    )
    stored = TableRef.from_dotted("dev", "silver.price$")

    with world.session() as session:
        authz = AllowAllAuthorization()
        catalog = CatalogService(session, StorageResolver(session), authz)
        rebuild = TableRebuild(session, catalog, RunService(session, authz), authz, undo_days=7)
        rebuilt = rebuild.rebuild(world.admin, stored, reason="Rebuild the stored name")
    assert not _exists(world, "silver.price$")
    _drain(world)

    task = _task(world, "silver.price$")
    assert (task.run_id, task.status) == (rebuilt.run_id, TaskStatus.SUCCEEDED)
    assert _exists(world, "silver.price$")
