"""The exit test: a second principal authenticates for real.

Real Postgres via testcontainers, a live uvicorn server in a background
thread, mirroring `test_lifecycle_integration.py`'s own pattern. If this
passes, more than one identity can exist and prove who it is,
through the real API, not a direct service call.
"""

import socket
import threading
import time

import pytest
import requests
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import Base, Principal
from lakana_server.app import create_app
from lakana_server.settings import Settings

pytestmark = pytest.mark.integration


def _free_port() -> int:
    """Find an available TCP port on localhost.

    Returns
    -------
    int
        A port not currently bound, suitable for uvicorn to listen on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Not held: uvicorn must be able to listen on this port.
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_second_principal_creates_a_pat_and_authenticates_with_it() -> None:
    """A service account, created and issued a PAT via the API, authenticates with it."""
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="exitcriterion",
            bootstrap_pat_secret="faketoken",
        )
        schema_engine = create_engine(settings.database)
        Base.metadata.create_all(schema_engine)
        schema_engine.dispose()

        app = create_app(settings)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "uvicorn did not start within 10 seconds"

            base_url = f"http://127.0.0.1:{port}"
            admin_headers = {"Authorization": "Bearer lakana_pat_exitcriterion_faketoken"}

            created = requests.post(
                f"{base_url}/api/v1/principals",
                headers=admin_headers,
                json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
                timeout=10,
            )
            assert created.status_code == 201, created.text

            # Grant.granted_by is a non-nullable foreign key onto
            # principals.id, so the acting PrincipalRef below must carry a
            # real, already-persisted row's id — the bootstrap admin,
            # created at app startup, is exactly that row. A fabricated
            # uuid.uuid4() (as AllowAllAuthorization's identity-skipping
            # would otherwise allow) violates that foreign key.
            engine = create_engine(settings.database)
            with Session(engine) as session:
                ci_bot_row = session.execute(
                    select(Principal).where(Principal.name == "ci-bot")
                ).scalar_one()
                admin_row = session.execute(
                    select(Principal).where(Principal.name == "admin")
                ).scalar_one()
                GrantService(session, AllowAllAuthorization()).grant(
                    PrincipalRef(id=admin_row.id, name=admin_row.name, type=admin_row.type),
                    principal=ci_bot_row.name,
                    role=Role.ADMIN,
                    kind=SecurableKind.INSTANCE,
                )
            engine.dispose()

            issued = requests.post(
                f"{base_url}/api/v1/principals/ci-bot/tokens",
                headers=admin_headers,
                json={"name": "ci"},
                timeout=10,
            )
            assert issued.status_code == 201, issued.text
            new_token = issued.json()["token"]

            # The real proof: authenticate as ci-bot, not the bootstrap admin.
            as_ci_bot = requests.get(
                f"{base_url}/api/v1/principals",
                headers={"Authorization": f"Bearer {new_token}"},
                timeout=10,
            )
            assert as_ci_bot.status_code == 200
            names = {p["name"] for p in as_ci_bot.json()}
            assert {"admin", "ci-bot"} <= names

            # A task token, minted via the API, likewise authenticates.
            minted = requests.post(
                f"{base_url}/api/v1/task-tokens",
                headers=admin_headers,
                json={
                    "principal": "ci-bot",
                    "environment": "dev",
                    "asset_scope": ["gold.sales"],
                    "ttl_seconds": 300,
                },
                timeout=10,
            )
            assert minted.status_code == 201, minted.text
            task_token = minted.json()["token"]

            # Proves the task token authenticated (only a resolved, real
            # principal can be told "no") without claiming it can exercise
            # an instance-scoped action — task-scope narrowing refuses that
            # unconditionally, by design, regardless of ci-bot's own grants.
            as_task = requests.get(
                f"{base_url}/api/v1/principals",
                headers={"Authorization": f"Bearer {task_token}"},
                timeout=10,
            )
            assert as_task.status_code == 403

            # A revoked PAT can no longer authenticate.
            key_id = issued.json()["key_id"]
            revoked = requests.delete(
                f"{base_url}/api/v1/principals/ci-bot/tokens/{key_id}",
                headers=admin_headers,
                timeout=10,
            )
            assert revoked.status_code == 204

            after_revoke = requests.get(
                f"{base_url}/api/v1/principals",
                headers={"Authorization": f"Bearer {new_token}"},
                timeout=10,
            )
            assert after_revoke.status_code == 401
        finally:
            server.should_exit = True
            thread.join(timeout=10)
