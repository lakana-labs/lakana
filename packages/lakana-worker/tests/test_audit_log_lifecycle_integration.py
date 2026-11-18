"""The exit test: every audited category is recorded and readable via the API.

Postgres via testcontainers, mirroring `test_policy_lifecycle_integration.py`'s
and `test_secrets_lifecycle_integration.py`'s own pattern exactly. Drives a
failed authentication, a denied action, a grant, a secret rotation, and a
query run to completion through the real API and a real query execution,
then reads the audit log back via `GET /api/v1/audit-events` as an admin and
confirms every one of the six events appears with the right principal,
type, environment, and detail -- proving the whole chain (seam-wrapped
events, explicitly-called events, and the read path's own authorization)
works end to end, not just that each piece works in isolation. Also
confirms the audit log itself never echoes a secret's rotated plaintext.
"""

import base64
import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer
from worker_test_helpers import SeaweedFsContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.bootstrap import ensure_bootstrap_admin_grant, ensure_bootstrap_principal
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.pats import PatService
from lakana_core.models import Base, Principal
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one

pytestmark = pytest.mark.integration

_PAT_KEY_ID = "auditcriterion"
_PAT_SECRET = "faketoken"  # nosec B105 -- a test fixture value, not a real credential
_TOKEN = f"lakana_pat_{_PAT_KEY_ID}_{_PAT_SECRET}"


def _master_key() -> str:
    """Return a fixed, valid base64-encoded master key for this test.

    Returns
    -------
    str
        A base64-encoded 32-byte key, suitable for
        `lakana_core.config.SecretsConfig.master_key`.
    """
    return base64.b64encode(b"7" * 32).decode()


def _sample_customers_parquet_bytes() -> bytes:
    """Serialize a tiny customers table to Parquet bytes.

    Returns
    -------
    bytes
        A valid Parquet file.
    """
    buffer = io.BytesIO()
    pq.write_table(pa.table({"id": [1, 2, 3]}), buffer)
    return buffer.getvalue()


def test_every_audited_category_is_recorded_and_readable_via_the_api() -> None:
    """A failed auth, a denial, a grant, a secret rotation, and a query all show up.

    Checks real attribution (the right principal, securable, and detail) for
    each of the six event types, not merely that an event of that type
    exists somewhere -- and that the audit log never echoes a secret's
    rotated plaintext value back to its reader.
    """
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        client = seaweedfs.get_client()
        client.make_bucket("lakana")
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"

        with Session(engine) as session:
            admin = ensure_bootstrap_principal(session, "admin")
            ensure_bootstrap_admin_grant(session, admin, AllowAllAuthorization())
            # The admin already has an active grant by the time the app
            # below starts, so `bootstrap_if_needed` finds an admin
            # and mints nothing -- this test mints the deterministic PAT
            # itself instead of relying on the app's lifespan to do it.
            PatService(session, AllowAllAuthorization()).issue(
                admin, admin.name, name="bootstrap", key_id=_PAT_KEY_ID, secret=_PAT_SECRET
            )
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            catalog.create_environment(
                admin,
                name="dev",
                storage=StorageConfig(
                    scheme="s3",
                    root="s3://lakana/dev",
                    options={
                        "s3.endpoint": endpoint,
                        "s3.access-key-id": seaweedfs.access_key,
                        "s3.secret-access-key": seaweedfs.secret_key,
                    },
                ),
            )
            sample_bytes = _sample_customers_parquet_bytes()
            client.put_object(
                "lakana",
                "dev/raw/customers/a.parquet",
                io.BytesIO(sample_bytes),
                length=len(sample_bytes),
            )
            ref = TableRef(
                environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
            )
            catalog.create_external_table(
                admin, ref, location_pattern="raw/customers/*.parquet", format="parquet"
            )

            settings = Settings(
                database=DatabaseConfig(url=pg.get_connection_url()),
                bootstrap_pat_key_id=_PAT_KEY_ID,
                bootstrap_pat_secret=_PAT_SECRET,
            )
            settings.secrets.master_key = SecretStr(_master_key())
            with TestClient(create_app(settings)) as api:
                admin_headers = {"Authorization": f"Bearer {_TOKEN}"}

                # 1. A failed authentication -- wrong secret against a real key_id.
                failed = api.get(
                    "/api/v1/audit-events",
                    headers={"Authorization": f"Bearer lakana_pat_{_PAT_KEY_ID}_wrongsecret"},
                )
                assert failed.status_code == 401

                # 2. A denied action -- alice has no grant at all yet.
                created_principal = api.post(
                    "/api/v1/principals",
                    headers=admin_headers,
                    json={"name": "alice", "type": "USER"},
                )
                assert created_principal.status_code == 201, created_principal.text
                alice_pat = api.post(
                    "/api/v1/principals/alice/tokens",
                    headers=admin_headers,
                    json={"name": "alice-ci"},
                )
                assert alice_pat.status_code == 201, alice_pat.text
                alice_headers = {"Authorization": f"Bearer {alice_pat.json()['token']}"}
                denied = api.post(
                    "/api/v1/grants",
                    headers=alice_headers,
                    json={
                        "principal": "alice",
                        "role": "ANALYST",
                        "kind": "ENVIRONMENT",
                        "environment": "dev",
                    },
                )
                assert denied.status_code == 403

                # 3. A grant -- admin grants alice ANALYST on dev.
                grant_response = api.post(
                    "/api/v1/grants",
                    headers=admin_headers,
                    json={
                        "principal": "alice",
                        "role": "ANALYST",
                        "kind": "ENVIRONMENT",
                        "environment": "dev",
                    },
                )
                assert grant_response.status_code == 201, grant_response.text

                # 4. A secret creation + rotation.
                secret_response = api.post(
                    "/api/v1/environments/dev/secrets",
                    headers=admin_headers,
                    json={"name": "s3-creds", "values": {"k": "v"}},
                )
                assert secret_response.status_code == 201, secret_response.text
                rotate_response = api.patch(
                    "/api/v1/environments/dev/secrets/s3-creds",
                    headers=admin_headers,
                    json={"values": {"k": "v2"}},
                )
                assert rotate_response.status_code == 200, rotate_response.text

            # 5. A query submitted and run to completion. PrincipalOut carries no
            # `id` field (only name/type/is_active), so alice's real id -- needed
            # for a direct PrincipalRef -- is read back from the database rather
            # than parsed from the REST response.
            alice_row = session.execute(
                select(Principal).where(Principal.name == "alice")
            ).scalar_one()
            alice_ref = PrincipalRef(id=alice_row.id, name=alice_row.name, type=alice_row.type)
            query_service = QueryService(session, GrantAuthorization(session))
            run = query_service.submit_query(alice_ref, "dev", "SELECT * FROM gold.customers")
            run_one(session, run.id, GrantAuthorization(session))
            session.refresh(run)
            assert run.status.value == "SUCCEEDED", run.error_message

            with TestClient(create_app(settings)) as api:
                events_response = api.get("/api/v1/audit-events", headers=admin_headers)
                assert events_response.status_code == 200
                assert events_response.json()["next_cursor"] is None
                events = events_response.json()["items"]
                event_types = {e["event_type"] for e in events}
                assert "AUTHENTICATION_FAILED" in event_types
                assert "AUTHORIZATION_DENIED" in event_types
                assert "GRANT_CREATED" in event_types
                assert "SECRET_CREATED" in event_types
                assert "SECRET_ROTATED" in event_types
                assert "QUERY_COMPLETED" in event_types

                def _of_type(event_type: str) -> list[dict]:
                    """Filter `events` down to one event type.

                    Parameters
                    ----------
                    event_type
                        The `AuditEventType` value to filter for.

                    Returns
                    -------
                    list of dict
                        Every event of that type, in the order returned by
                        the API.
                    """
                    return [e for e in events if e["event_type"] == event_type]

                # AUTHENTICATION_FAILED: no principal was ever verified, but
                # the attempted key_id is still recorded.
                assert any(
                    e["principal_id"] is None and e["detail"]["key_id"] == _PAT_KEY_ID
                    for e in _of_type("AUTHENTICATION_FAILED")
                ), _of_type("AUTHENTICATION_FAILED")

                # AUTHORIZATION_DENIED: attributed to alice, targeting the
                # INSTANCE-scoped MANAGE_GRANT check her own grant attempt
                # failed -- not just any denial from setup noise.
                assert any(
                    e["principal_id"] == str(alice_row.id) and e["securable_kind"] == "INSTANCE"
                    for e in _of_type("AUTHORIZATION_DENIED")
                ), _of_type("AUTHORIZATION_DENIED")

                # GRANT_CREATED: discriminated on the REST grant's own
                # specific shape (alice/ANALYST/dev) -- bootstrap's own
                # admin grant also emits a GRANT_CREATED event
                # (INSTANCE/admin/ADMIN), so asserting type membership alone
                # would pass even if the REST grant path recorded nothing.
                assert any(
                    e["detail"]["principal"] == "alice"
                    and e["detail"]["role"] == "ANALYST"
                    and e["securable_environment"] == "dev"
                    for e in _of_type("GRANT_CREATED")
                ), _of_type("GRANT_CREATED")

                # SECRET_CREATED / SECRET_ROTATED: the right secret name, in
                # the right environment.
                assert any(
                    e["detail"]["name"] == "s3-creds" and e["securable_environment"] == "dev"
                    for e in _of_type("SECRET_CREATED")
                ), _of_type("SECRET_CREATED")
                assert any(
                    e["detail"]["name"] == "s3-creds" and e["securable_environment"] == "dev"
                    for e in _of_type("SECRET_ROTATED")
                ), _of_type("SECRET_ROTATED")

                # QUERY_COMPLETED: attributed to the principal that actually
                # ran the query.
                assert any(
                    e["principal_id"] == str(alice_row.id) for e in _of_type("QUERY_COMPLETED")
                ), _of_type("QUERY_COMPLETED")

                # The audit log must never echo a secret's rotated plaintext
                # back to its reader -- the one test in this suite that
                # creates a real secret with real values and then reads the
                # entire log back as JSON, so this is its most load-bearing
                # negative assertion.
                assert "v2" not in events_response.text

                unauthorized = api.get("/api/v1/audit-events", headers=alice_headers)
                assert unauthorized.status_code == 403

        engine.dispose()
