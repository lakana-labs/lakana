"""No REST request can set a run context or a write fence (a rule of the run engine).

The break-glass reason is deliberately allowed on the wire;
the guard authorizes and audits it. Only what a request can send is checked: the parameters
and request bodies of every operation, with the schemas they refer to. A response may name a
task run (an artifact version says which task run committed it); that sets nothing.
"""

import shutil
from pathlib import Path

import pytest

import lakana_server
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import _TEST_MASTER_KEY

_FORBIDDEN = {"runcontext", "taskrunid", "claim", "writefence", "workerid"}
"""Names a request must not carry, compared in `_squash` form: `task-run-id`, `task_run_id` and
`TaskRunId` are one name. `claim` is included because the only claim in play is a task run's
lease counter, which a request must never set; no request parameter or body field of the
platform contains it today, and the test fails when one appears. `writefence` and `workerid`
are the write fence's two halves: only the worker, for the run it executes, sets them."""

_ALLOWED = {"claims"}
"""Names that contain a forbidden one and are not it, compared exactly in `_squash` form. `claims`
is the body field of `POST /api/v1/ci-trust-rules/explain`: the decoded claims of a CI job's OIDC
ID token, which an administrator pastes to test the trust rules, not a task run's claim counter."""


@pytest.fixture(scope="module")
def openapi_schema(
    tmp_path_factory: pytest.TempPathFactory, catalog_template: Path
) -> dict[str, object]:
    """Generate the OpenAPI document once for the module.

    Building it walks every route and costs over a second; no test here changes the app, so
    one app serves them all. The document is read from the app, which is what `/openapi.json`
    serves, without the lifespan or an HTTP round trip.

    Parameters
    ----------
    tmp_path_factory
        Pytest's session-wide temporary directory factory.
    catalog_template
        The session's read-only database with the schema already created.

    Returns
    -------
    dict
        The OpenAPI document of a fully constructed app.
    """
    database = tmp_path_factory.mktemp("openapi") / "lakana.db"
    shutil.copyfile(catalog_template, database)
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{database}"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
    )
    return create_app(settings).openapi()


def _squash(name: str) -> str:
    """Reduce a parameter or field name to what FastAPI and clients treat as one name.

    Parameters
    ----------
    name
        The name as the OpenAPI document spells it (a header is spelled with dashes).

    Returns
    -------
    str
        Lower case, without `-` and `_`.
    """
    return name.lower().replace("-", "").replace("_", "")


def forbidden_in(names: set[str]) -> set[str]:
    """Pick the names that contain a forbidden one, as a substring.

    Parameters
    ----------
    names
        Names a request can carry.

    Returns
    -------
    set[str]
        The offending names, as written.
    """
    return {
        name
        for name in names
        if _squash(name) not in _ALLOWED and any(bad in _squash(name) for bad in _FORBIDDEN)
    }


def _walk(
    node: object, components: dict[str, object], seen: frozenset[str] = frozenset()
) -> set[str]:
    """Collect every property name below an OpenAPI node, following `$ref`s.

    Parameters
    ----------
    node
        A JSON-decoded OpenAPI fragment.
    components
        The document's `components.schemas`, to resolve a `$ref`.
    seen
        The schemas already being followed, so a recursive one ends.

    Returns
    -------
    set[str]
        Every key of every `properties` mapping below `node`, and every
        parameter name.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            found |= set(props)
        if isinstance(node.get("name"), str) and "in" in node:
            found.add(node["name"])
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in seen and ref.rsplit("/", 1)[-1] in components:
            found |= _walk(components[ref.rsplit("/", 1)[-1]], components, seen | {ref})
        for value in node.values():
            found |= _walk(value, components, seen)
    elif isinstance(node, list):
        for item in node:
            found |= _walk(item, components, seen)
    return found


def request_names(schema: dict[str, object]) -> set[str]:
    """Collect every name a request can carry: parameters and request bodies.

    Parameters
    ----------
    schema
        The OpenAPI document.

    Returns
    -------
    set[str]
        The names.
    """
    components = schema.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    found: set[str] = set()
    paths = schema.get("paths", {})
    for operations in paths.values() if isinstance(paths, dict) else []:
        found |= _walk(operations.get("parameters"), schemas)  # shared by every operation
        for operation in operations.values():
            if isinstance(operation, dict):
                for key in ("parameters", "requestBody"):
                    found |= _walk(operation.get(key), schemas)
    return found


def test_the_openapi_schema_has_no_run_context_input(openapi_schema: dict[str, object]) -> None:
    """The run context is set only by the worker: no schema, query, header or path takes it."""
    names = request_names(openapi_schema)
    assert names, "the OpenAPI walk found nothing"
    assert forbidden_in(names) == set()


def test_the_walk_finds_a_forbidden_name_in_a_request_and_ignores_a_response() -> None:
    """The guard has teeth for inputs, through a `$ref`, and lets a response name a task run."""
    document: dict[str, object] = {
        "paths": {
            "/a": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/In"}}
                        }
                    },
                    "responses": {"200": {"schema": {"$ref": "#/components/schemas/Out"}}},
                }
            }
        },
        "components": {
            "schemas": {
                "In": {"properties": {"inner": {"$ref": "#/components/schemas/Inner"}}},
                "Inner": {"properties": {"run_context": {}}},
                "Out": {"properties": {"task_run_id": {}}},
            }
        },
    }
    assert request_names(document) == {"inner", "run_context"}
    assert forbidden_in(request_names(document)) == {"run_context"}
    del document["components"]["schemas"]["Inner"]
    assert request_names(document) == {"inner"}


def test_a_header_a_query_parameter_and_a_path_level_parameter_are_found_by_any_spelling() -> None:
    """FastAPI spells `task_run_id: str = Header("")` as `task-run-id`."""
    document: dict[str, object] = {
        "paths": {
            "/a": {
                "parameters": [{"name": "X-Run-Context", "in": "header"}],
                "get": {"parameters": [{"name": "task-run-id", "in": "header"}]},
                "put": {"parameters": [{"name": "taskRunId", "in": "query"}]},
                "post": {"parameters": [{"name": "claim_token", "in": "query"}]},
                "delete": {"parameters": [{"name": "limit", "in": "query"}]},
            }
        }
    }
    assert forbidden_in(request_names(document)) == {
        "X-Run-Context",
        "task-run-id",
        "taskRunId",
        "claim_token",
    }


def test_only_the_exact_allowed_name_escapes_the_guard() -> None:
    """`claims` (a CI ID token's claims) passes; a claim counter by any other spelling does not."""
    assert forbidden_in({"claims", "claim", "task_claim", "claims_count", "Claim"}) == {
        "claim",
        "task_claim",
        "claims_count",
        "Claim",
    }


def test_no_server_code_builds_a_run_context() -> None:
    """The OpenAPI walk cannot see a raw-body route; the source can: only the worker builds one.

    The write fence (`WriteFence`) is held to the same rule.
    """
    root = Path(lakana_server.__file__).parent
    builders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in ("RunContext(", "WriteFence"))
    )
    assert builders == []


def test_no_route_can_roll_a_table_back(openapi_schema: dict[str, object]) -> None:
    """Nothing a request sends reaches the rollback or its exemption.

    `CatalogService.roll_back` is called by the worker for a running task; `commit_table`'s
    `internal_rollback` is its one exemption from the rule that a commit cannot remove a
    table's current snapshot. Neither appears in the server's sources, and no request schema
    has a field of that name.
    """
    root = Path(lakana_server.__file__).parent
    callers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(
            name in path.read_text(encoding="utf-8")
            for name in ("roll_back(", "internal_rollback", "after_swap")
        )
    )
    assert callers == []
    names = request_names(openapi_schema)
    assert {name for name in names if "rollback" in _squash(name)} == set()
