"""The exit test: a secret created via the API backs an environment's real storage.

Postgres and SeaweedFS via testcontainers, mirroring
`test_worker_lifecycle_integration.py`'s and
`test_policy_lifecycle_integration.py`'s own patterns exactly, including
the same `with TestClient(create_app(settings)) as api:` idiom -- using
`TestClient` as a context manager is what actually triggers the app's ASGI
lifespan (which mints the bootstrap admin's deterministic PAT from
`Settings.bootstrap_pat_key_id`/`bootstrap_pat_secret`); a bare
`TestClient(app)` never runs it, and every request would then 401. Creates
the environment's storage secret through the real REST API (not a direct
service call), then proves a real query against that environment only
succeeds because the secret's real, decrypted credentials authenticate to
the real SeaweedFS container -- and that a deliberately wrong secret value
fails cleanly, with its own value never appearing in the resulting error.
"""

import base64
import io

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer
from worker_test_helpers import SeaweedFsContainer

from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Base, QueryRunStatus
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one

pytestmark = pytest.mark.integration

_PAT_KEY_ID = "exitcriterion"
_PAT_SECRET = "faketoken"
_TOKEN = f"lakana_pat_{_PAT_KEY_ID}_{_PAT_SECRET}"
"""The bootstrap admin's PAT, minted deterministically by the app's own
lifespan from `Settings.bootstrap_pat_key_id`/`bootstrap_pat_secret` --
matching `test_policy_lifecycle_integration.py` exactly."""


def _master_key() -> str:
    """Return a fixed, valid base64-encoded master key for this test.

    Returns
    -------
    str
        A base64-encoded 32-byte key, suitable for
        `lakana_core.config.SecretsConfig.master_key`.
    """
    return base64.b64encode(b"7" * 32).decode()


def test_a_secret_created_via_the_api_backs_a_real_environments_storage() -> None:
    """A real query succeeds against a secret-backed environment, and a wrong secret fails."""
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        db_url = pg.get_connection_url()
        engine = create_engine(DatabaseConfig(url=db_url))
        Base.metadata.create_all(engine)
        real_config = seaweedfs.get_config()

        settings = Settings(
            database=DatabaseConfig(url=db_url),
            bootstrap_pat_key_id=_PAT_KEY_ID,
            bootstrap_pat_secret=_PAT_SECRET,
        )
        settings.secrets.master_key = SecretStr(_master_key())
        headers = {"Authorization": f"Bearer {_TOKEN}"}

        # Everything the exit criterion requires "created via the API"
        # happens inside this one `with` block, against the real app --
        # the lifespan mints the admin's principal, grant, and PAT here,
        # on first entry, since this is a fresh database.
        with TestClient(create_app(settings)) as api:
            created = api.post(
                "/api/v1/environments",
                headers=headers,
                json={"name": "dev", "storage": {"scheme": "s3", "root": "s3://lakana/dev"}},
            )
            assert created.status_code == 201, created.text

            secret_created = api.post(
                "/api/v1/environments/dev/secrets",
                headers=headers,
                json={
                    "name": "s3-creds",
                    "values": {
                        "s3.endpoint": f"http://{real_config['endpoint']}",
                        "s3.access-key-id": real_config["access_key"],
                        "s3.secret-access-key": real_config["secret_key"],
                    },
                },
            )
            assert secret_created.status_code == 201, secret_created.text

            storage_updated = api.patch(
                "/api/v1/environments/dev/storage", headers=headers, json={"secret_ref": "s3-creds"}
            )
            assert storage_updated.status_code == 200, storage_updated.text

        s3_client = seaweedfs.get_client()
        s3_client.make_bucket("lakana")
        sample = io.BytesIO()
        pq.write_table(pa.table({"id": [1, 2]}), sample)
        sample_bytes = sample.getvalue()
        s3_client.put_object(
            "lakana", "dev/raw/events/a.parquet", io.BytesIO(sample_bytes), length=len(sample_bytes)
        )

        # Registering the external table, submitting, and running the query
        # have no REST surface (matching the policy exit-criterion test, which
        # hits this exact same limitation) -- done directly, but the
        # storage credentials it needs were created entirely over the wire
        # above, and are decrypted for real here via the same provider.
        provider = EnvKeyProvider(settings.secrets.master_key_text())
        with Session(engine) as session:
            admin = ensure_bootstrap_principal(session, settings.bootstrap_admin)
            catalog = CatalogService(
                session, StorageResolver(session, provider), GrantAuthorization(session)
            )
            catalog.create_namespace(admin, "dev", NamespacePath.from_parts(["raw"]))
            catalog.create_external_table(
                admin,
                TableRef(
                    environment="dev",
                    namespace=NamespacePath.from_parts(["raw"]),
                    name="events",
                ),
                location_pattern="raw/events/*.parquet",
                format="parquet",
            )

            query_service = QueryService(session, GrantAuthorization(session))
            run = query_service.submit_query(admin, "dev", "SELECT id FROM raw.events")

            run_one(session, run.id, GrantAuthorization(session), provider)

            session.refresh(run)
            assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
            assert run.result_location is not None
            resolved = StorageResolver(session, provider).for_environment("dev")
            raw = resolved.storage.read(run.result_location)
            with pyarrow.ipc.open_file(io.BytesIO(raw)) as reader:
                assert reader.read_all().to_pydict() == {"id": [1, 2]}

        # Rotate to a deliberately wrong secret value, again through the
        # real API -- a second `with TestClient(...)` against the same
        # database is safe: the first start already made the bootstrap
        # principal an active admin, so `bootstrap_if_needed` finds
        # one and is a no-op this time -- no re-grant, no new PAT, and the
        # one already minted keeps working.
        with TestClient(create_app(settings)) as api:
            wrong = api.patch(
                "/api/v1/environments/dev/secrets/s3-creds",
                headers=headers,
                json={
                    "values": {
                        "s3.endpoint": f"http://{real_config['endpoint']}",
                        "s3.access-key-id": real_config["access_key"],
                        "s3.secret-access-key": "WRONG_SECRET_MARKER_MUST_NOT_LEAK",
                    }
                },
            )
            assert wrong.status_code == 200, wrong.text

        with Session(engine) as session:
            query_service = QueryService(session, GrantAuthorization(session))
            run2 = query_service.submit_query(admin, "dev", "SELECT id FROM raw.events")
            run_one(session, run2.id, GrantAuthorization(session), provider)
            session.refresh(run2)
            assert run2.status == QueryRunStatus.FAILED
            assert (
                run2.error_message
                == "Resolving the tables this query reads failed (PermissionError)."
            )

        engine.dispose()
