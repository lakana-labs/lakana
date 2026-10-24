"""End-to-end test for the SDK's admin resources, through the real server.

Drives the full identity/authorization/policy lifecycle through
`lakana.Client` only: principal -> PAT -> grant -> group -> row filter/column
mask -> task token -> secret -> audit log -> revocation. Queries execute
in-process via `lakana_worker.pipeline.run_one`, matching
`test_sdk_data_plane.py`'s established pattern.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import lakana
from lakana_core.grants.authorization import GrantAuthorization
from lakana_worker.pipeline import run_one


def _run_worker_once(client: TestClient, query_id: str) -> None:
    """Run one already-queued query run to completion, in-process.

    Parameters
    ----------
    client
        The test client, used to reach the app's database engine.
    query_id
        The submitted query's id, as returned by the SDK.
    """
    engine = client.app.state.engine
    with Session(engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))


@pytest.mark.slow
def test_sdk_admin_lifecycle(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """The full admin workflow, driven only through the SDK.

    1. Create principal `bob`.
    2. Issue bob a PAT, build a second `Client` as bob.
    3. Grant bob VIEWER on `dev`.
    4. As bob: list environments (sees `dev`); append is `PermissionDenied`.
    5. Admin creates a group, adds bob, grants the group DATA_SCIENTIST on
       `bronze`. As bob, append now succeeds.
    6. A row filter and a column mask on the table restrict/mask what bob
       reads back, via `run_one`.
    7. A task token scoped to `bronze.*` is minted and used via a third
       `Client`.
    8. A secret is created, then rotated. Its value is never returned.
    9. `audit_events.list(event_type="TABLE_APPENDED")` returns the append.
    10. Everything is revoked, and bob's own calls then fail.
    """
    # --- Setup: environment and table, as admin ---------------------------
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)

    # --- Step 1: create principal bob --------------------------------------
    bob = lakana_client.principals.create("bob", "USER")
    assert bob.name == "bob"
    assert bob.is_active is True

    # --- Step 2: issue bob a PAT, build a second Client --------------------
    issued = lakana_client.tokens.issue("bob", "bob-laptop")
    assert issued.token.startswith("lakana_pat_")
    bob_client = lakana.Client(url="http://testserver", token=issued.token, http_client=client)

    listed_tokens = lakana_client.tokens.list("bob")
    assert [t.key_id for t in listed_tokens] == [issued.key_id]

    # --- Step 3: grant bob VIEWER on dev ------------------------------------
    viewer_grant = lakana_client.grants.grant(
        principal="bob", role="VIEWER", kind="ENVIRONMENT", environment="dev"
    )
    assert viewer_grant.role == "VIEWER"

    # --- Step 4: as bob, list sees dev; append is denied -------------------
    envs = bob_client.environments.list()
    assert any(e.name == "dev" for e in envs)

    with pytest.raises(lakana.PermissionDenied):
        bob_client.tables.append(
            "dev", "bronze.sales", pa.table({"id": [1], "region": ["eu"]}, schema=schema)
        )

    # --- Step 5: group grant unlocks append ---------------------------------
    group = lakana_client.groups.create("data-science")
    assert group.name == "data-science"
    lakana_client.groups.add_member("data-science", "bob")
    assert [m.name for m in lakana_client.groups.list_members("data-science")] == ["bob"]
    group_grant = lakana_client.grants.grant(
        group="data-science", role="DATA_SCIENTIST", kind="ENVIRONMENT", environment="dev"
    )
    assert group_grant.group_id is not None

    data = pa.table({"id": [1, 2], "region": ["eu", "us"]}, schema=schema)
    appended = bob_client.tables.append("dev", "bronze.sales", data)
    assert appended.rows == 2

    # --- Step 6: row filter + column mask restrict what bob reads ----------
    row_filter = lakana_client.row_filters.create(
        principal="bob", environment="dev", namespace="bronze", table="sales", expression="id > 1"
    )
    assert row_filter.expression == "id > 1"
    column_mask = lakana_client.column_masks.create(
        principal="bob",
        environment="dev",
        namespace="bronze",
        table="sales",
        column="region",
        expression="'MASKED'",
    )
    assert column_mask.column == "region"

    run = bob_client.queries.submit("SELECT id, region FROM bronze.sales ORDER BY id", "dev")
    _run_worker_once(client, run.id)
    result = run.result()
    assert result.column("id").to_pylist() == [2]
    assert result.column("region").to_pylist() == ["MASKED"]

    # --- Step 7: a task token, used via a third Client ----------------------
    minted = lakana_client.task_tokens.mint("bob", "dev", ["bronze.*"], 300)
    assert minted.token.startswith("lakana_task_")
    task_client = lakana.Client(url="http://testserver", token=minted.token, http_client=client)

    task_run = task_client.queries.submit("SELECT count(*) AS n FROM bronze.sales", "dev")
    _run_worker_once(client, task_run.id)
    task_result = task_run.result()
    # The task token authenticates as bob, so bob's own row filter
    # ("id > 1") still applies -- only one of the two appended rows.
    assert task_result.column("n").to_pylist() == [1]

    # --- Step 8: create, then rotate, a secret ------------------------------
    secret = lakana_client.secrets.create("dev", "s3-creds", {"access_key_id": "AKIA123"})
    assert secret.name == "s3-creds"
    assert not hasattr(secret, "values")
    assert not hasattr(secret, "value")
    rotated = lakana_client.secrets.rotate("dev", "s3-creds", {"access_key_id": "AKIANEW"})
    assert rotated.name == "s3-creds"
    assert "AKIANEW" not in repr(rotated)
    listed_secrets = lakana_client.secrets.list("dev")
    assert [s.name for s in listed_secrets] == ["s3-creds"]

    # --- Step 9: the append shows up in the audit log -----------------------
    append_events = lakana_client.audit_events.list(event_type="TABLE_APPENDED").items
    assert any(
        e.securable_environment == "dev" and e.securable_table == "sales" for e in append_events
    )

    # --- Step 10: revoke everything, then bob's own calls fail --------------
    lakana_client.row_filters.revoke(row_filter.id)
    lakana_client.column_masks.revoke(column_mask.id)
    lakana_client.grants.revoke(viewer_grant.id)
    lakana_client.grants.revoke(group_grant.id)
    lakana_client.secrets.revoke("dev", "s3-creds")
    lakana_client.tokens.revoke("bob", issued.key_id)
    lakana_client.task_tokens.revoke(minted.key_id)

    with pytest.raises(lakana.AuthenticationError):
        task_client.environments.list()

    with pytest.raises(lakana.AuthenticationError):
        bob_client.environments.list()
