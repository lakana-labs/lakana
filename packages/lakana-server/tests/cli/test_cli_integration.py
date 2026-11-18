"""CLI round trip against real Postgres and SeaweedFS.

Every other CLI end-to-end test in this directory runs against SQLite and
`file://` storage (see `conftest.py`'s `client`/`worker` fixtures and
`test_cli_end_to_end.py`). This test instead stands up a real
`postgres:16-alpine` and a real SeaweedFS S3 gateway via testcontainers —
matching `test_sdk_integration.py`'s pattern — to prove the CLI's own
login/create/append/query round trip also works against the
production-shaped catalog and object-storage backends, not only the fast
unit-tier doubles.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import Base
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.poller import poll_once
from tests.cli.conftest import ADMIN_TOKEN, make_cli_state, run_cli
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration


def test_cli_round_trip_against_postgres_and_seaweedfs(tmp_path: Path) -> None:
    """Login, create env/table, append a CSV, and query, over real backends.

    Drives the platform through `lakana`'s own Typer app, invoked in-process
    against the app's `TestClient` — no real network hop needed, matching
    `test_sdk_integration.py`'s approach for the SDK. A query is run to
    completion by an inline background-thread worker loop, copied from
    `conftest.py`'s `worker` fixture.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory; holds the CLI's profile file and the
        CSV fixture appended to the table.
    """
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        bucket_client = seaweedfs.get_client()
        bucket_client.make_bucket("lakana")
        seaweedfs_endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="testkey",
            bootstrap_pat_secret="testsecret",
        )

        # The app's lifespan bootstraps the admin principal but does not run
        # migrations (a separate deploy step in production); create the
        # schema directly, matching `test_sdk_integration.py`.
        schema_engine = create_engine(settings.database)
        Base.metadata.create_all(schema_engine)
        schema_engine.dispose()

        with TestClient(create_app(settings)) as client:
            engine = client.app.state.engine
            stop = threading.Event()
            errors: list[BaseException] = []

            def loop() -> None:
                """Repeatedly claim and run one queued query until stopped.

                Any exception that escapes an iteration ends the loop and is
                recorded in `errors`, rather than dying silently on this
                daemon thread, matching `conftest.py`'s `worker` fixture.
                """
                try:
                    while not stop.is_set():
                        with Session(engine) as session:
                            claimed = poll_once(session, GrantAuthorization(session))
                        if not claimed:
                            stop.wait(0.05)
                except BaseException as exc:
                    errors.append(exc)

            worker_thread = threading.Thread(target=loop, daemon=True)
            worker_thread.start()
            try:
                state = make_cli_state(client, tmp_path)
                run = partial(run_cli, state)

                result = run(
                    "login",
                    "--url",
                    "http://testserver",
                    "--token-stdin",
                    "--env",
                    "dev",
                    input=ADMIN_TOKEN + "\n",
                )
                assert result.exit_code == 0, result.stderr

                result = run(
                    "env",
                    "create",
                    "dev",
                    "--storage-root",
                    "s3://lakana/dev",
                    "--option",
                    f"s3.endpoint={seaweedfs_endpoint}",
                    "--option",
                    f"s3.access-key-id={seaweedfs.access_key}",
                    "--option",
                    f"s3.secret-access-key={seaweedfs.secret_key}",
                )
                assert result.exit_code == 0, result.stderr

                result = run(
                    "table",
                    "create",
                    "bronze.sales",
                    "--column",
                    "id:long",
                    "--column",
                    "region:string?",
                )
                assert result.exit_code == 0, result.stderr

                csv = tmp_path / "sales.csv"
                csv.write_text("id,region\n1,eu\n2,us\n3,\n")
                result = run("table", "append", "bronze.sales", str(csv))
                assert result.exit_code == 0, result.stderr
                assert "Appended 3 rows to dev/bronze.sales" in result.stdout

                result = run(
                    "--output",
                    "json",
                    "sql",
                    "SELECT id, region FROM bronze.sales ORDER BY id",
                )
                assert result.exit_code == 0, result.stderr
                assert json.loads(result.stdout) == [
                    {"id": 1, "region": "eu"},
                    {"id": 2, "region": "us"},
                    {"id": 3, "region": None},
                ]
            finally:
                stop.set()
                worker_thread.join(timeout=10)
                assert not worker_thread.is_alive()
                if errors:
                    raise errors[0]
