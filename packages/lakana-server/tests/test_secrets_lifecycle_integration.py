"""Stored secret values are not in the secrets routes, the audit log, the secrets row or the log.

Real Postgres via testcontainers behind the real app. The values stored through the secrets
routes are unique strings. They are searched for in the body and headers of every response of
the secrets routes (create, list, rotate and their error cases), of every GET route that needs
no path parameter (the audit log among them), in the raw `secrets` row, in the raw audit
events and in the captured log. A hit means a value leaked.

The paths that use a secret (an environment that reaches its storage through one, the Iceberg
routes, a query, a storage call that fails) are in `test_secrets_use_integration.py`.
"""

import base64
import json
import logging
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.db import create_engine
from lakana_core.models import Base, Secret
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER

pytestmark = pytest.mark.integration

_MASTER_KEY = base64.b64encode(b"k" * 32).decode()


def test_secret_values_are_absent_from_the_secrets_routes_audit_log_row_and_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stored values are in no response, audit event or log line; the row holds them encrypted."""
    stored = f"stored-{uuid.uuid4().hex}"
    rotated = f"rotated-{uuid.uuid4().hex}"
    retried = f"retried-{uuid.uuid4().hex}"
    stored_values = (stored, rotated, retried)

    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="testkey",
            bootstrap_pat_secret="testsecret",
            secrets=SecretsConfig(master_key=_MASTER_KEY),
        )
        schema_engine = create_engine(settings.database)
        Base.metadata.create_all(schema_engine)
        schema_engine.dispose()

        app = create_app(settings)
        bodies: list[str] = []
        swept: list[str] = []
        caplog.set_level(logging.DEBUG)

        def seen(response: httpx.Response, expected: set[int]) -> None:
            assert response.status_code in expected, (
                f"{response.request.method} {response.request.url} gave {response.status_code}"
            )
            bodies.append(response.text)
            bodies.append(repr(sorted(response.headers.multi_items())))

        with TestClient(app) as client:
            env = client.post(
                "/api/v1/environments",
                headers=AUTH_HEADER,
                json={"name": "dev", "storage": {"scheme": "file", "root": f"file://{tmp_path}"}},
            )
            assert env.status_code == 201, env.text
            base = "/api/v1/environments/dev/secrets"

            created = client.post(
                base,
                headers=AUTH_HEADER,
                json={"name": "s3-creds", "values": {"access_key_id": stored}},
            )
            assert created.status_code == 201, created.text
            seen(created, {201})
            assert set(created.json()) == {
                "id",
                "name",
                "environment",
                "created_by",
                "created_at",
                "format_version",
                "key_id",
                "use",
                "use_target",
                "use_target_url",
            }

            listed = client.get(base, headers=AUTH_HEADER)
            assert listed.status_code == 200
            seen(listed, {200})
            assert [set(s) for s in listed.json()] == [set(created.json())]

            patched = client.patch(
                f"{base}/s3-creds", headers=AUTH_HEADER, json={"values": {"access_key_id": rotated}}
            )
            assert patched.status_code == 200, patched.text
            seen(patched, {200})
            assert set(patched.json()) == set(created.json())

            # A secret's own value is never a way to read it: there is no GET by name, and the
            # error paths name the secret, never its value.
            for response, status in (
                (
                    client.post(
                        base,
                        headers=AUTH_HEADER,
                        json={"name": "s3-creds", "values": {"access_key_id": retried}},
                    ),
                    409,
                ),
                (
                    client.patch(
                        f"{base}/missing",
                        headers=AUTH_HEADER,
                        json={"values": {"access_key_id": "x"}},
                    ),
                    404,
                ),
                (client.get(f"{base}/s3-creds", headers=AUTH_HEADER), 405),
                (
                    client.post(
                        "/api/v1/environments/missing/secrets",
                        headers=AUTH_HEADER,
                        json={"name": "n", "values": {"k": "v"}},
                    ),
                    404,
                ),
                (client.get(base), 401),
            ):
                seen(response, {status})

            # Every GET route that needs no path parameter, the audit log among them.
            # A list route that needs a filter answers 422 to a bare GET (no body worth searching);
            # anything else must answer 200, and enough routes must have.
            for path, operations in app.openapi()["paths"].items():
                if "get" in operations and "{" not in path and path.startswith("/api/"):
                    answer = client.get(path, headers=AUTH_HEADER)
                    seen(answer, {200, 422})
                    if answer.status_code == 200:
                        swept.append(path)
            audit = client.get("/api/v1/audit-events", headers=AUTH_HEADER, params={"limit": 500})
            assert audit.status_code == 200
            kinds = {event["event_type"] for event in audit.json()["items"]}
            assert {"SECRET_CREATED", "SECRET_ROTATED"} <= kinds
            seen(audit, {200})
            assert "/api/v1/audit-events" in swept
            assert len(swept) >= 5

            schema = app.openapi()["components"]["schemas"]["SecretOut"]
            assert set(schema["properties"]) == {
                "id",
                "name",
                "environment",
                "created_by",
                "created_at",
                "format_version",
                "key_id",
                "use",
                "use_target",
                "use_target_url",
            }

        engine = create_engine(settings.database)
        try:
            with Session(engine) as session:
                rows = session.execute(select(Secret)).scalars().all()
                assert len(rows) == 1
                row = rows[0]
                raw = [bytes(row.ciphertext), bytes(row.nonce), bytes(row.wrapped_data_key)]
                # Control: the row is the rotated value, encrypted. Decrypting it with the master
                # key gives it back, so the search below looks at real ciphertext.
                decrypted = decrypt_secret_row(
                    row, EnvKeyProvider(_MASTER_KEY), environment_name="dev"
                )
                assert (row.format_version, row.key_id) == (
                    2,
                    EnvKeyProvider(_MASTER_KEY).key_id,
                )
                assert decrypted == {"access_key_id": rotated}
                audit_rows = session.execute(text("SELECT detail FROM audit_events")).all()
                raw.extend(json.dumps(r[0]).encode() for r in audit_rows)
        finally:
            engine.dispose()

    # Controls: the search saw the secrets routes, the audit log and a real log capture.
    assert "POST http://testserver/api/v1/environments/dev/secrets" in caplog.text
    assert "s3-creds" in "".join(bodies)
    assert "SECRET_ROTATED" in "".join(bodies)
    master_key_bytes = b"k" * 32
    assert _MASTER_KEY not in "".join(bodies)
    for value in stored_values:
        needle = value.encode()
        assert all(value not in body for body in bodies), f"{value} was in a response"
        assert all(needle not in blob for blob in raw), f"{value} was in a raw row"
        assert needle not in caplog.text.encode(), f"{value} was in a log line"
    assert all(master_key_bytes not in blob for blob in raw)
    assert _MASTER_KEY not in caplog.text
