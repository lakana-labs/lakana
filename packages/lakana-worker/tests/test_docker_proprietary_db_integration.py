"""The SQL Server recipe of the documentation runs against Microsoft's own image (#56).

The two assets of ``docs/examples/proprietary-db`` run as tasks under the worker's Docker runner,
with the real ``lakana[assets]`` from the wheelhouse and the recipe's driver (``python-tds`` and
``pyOpenSSL``, exactly the pins of the example's ``pyproject.toml``) in their environment. The
server is SQL Server 2022 Developer edition (`_IMAGE`, pinned by digest; its licence allows "to
design, develop, test and demonstrate your programs", ``docs/agent/testing.md``) on the sources'
network of `test_docker_task_egress_integration`, whose stack, egress service and launcher this
module reuses. It serves TLS only (``forceencryption``) with a certificate of the module's
authority for `_HOST`; the task trusts it through the trust bundle, and its connection is a
``tcp`` connection, so the asset reaches it through the relay.

Each case runs a probe asset that loads the example's file next to it and calls its function,
prints what it returned and fails on purpose: the stand-in server takes no table. Passwords are
generated for each run.
"""

from __future__ import annotations

import json
import secrets
import time
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import docker_stack
import pytest
import test_docker_task_egress_integration as egress_cases
from docker_stack import PYTHON, Stack
from test_docker_task_egress_integration import (
    Sources,
    _address,
    _Mirrored,
    _serve_connections,
    _wheelhouse,
)
from tls_fixture import issue_leaf, make_authority

from lakana_core.connections.endpoints import endpoints
from lakana_core.connections.kinds import ConnectionKind
from lakana_worker.launcher.requests import environment_volume
from lakana_worker.launcher_client import LauncherClient
from lakana_worker.python_tasks import connection_plan
from lakana_worker.runners import DockerRunner, PythonTask, RunOutcome, StopSignal
from lakana_worker.trust import build_trust_bundle

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.slow]

sources = egress_cases.sources
"""The egress module's stack, source network and egress service, for this module."""
worker_side = egress_cases.worker_side
"""The worker's launcher client on that stack."""

_IMAGE = (
    "mcr.microsoft.com/mssql/server:2022-latest"
    "@sha256:cf8f0ab2ebe61feec2eed2629061a07d247f791c2a8ddda74a4ac9f06a291718"
)
"""SQL Server 2022, build 16.0.4305.5, linux/amd64 only; change the tag only with its digest."""

_SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
_HOST = "erp-db.lakana.test"
_OTHER_HOST = "erp-db-2.lakana.test"
"""A second name of the same server, which its certificate does not hold."""

_RECIPE = Path(__file__).resolve().parents[3] / "docs" / "examples" / "proprietary-db" / "project"
_ENVIRONMENT_KEY = "7" * 64
_DRIVER = ("python-tds==1.17.1", "pyopenssl==26.4.0")
"""The example's pins; `test_the_driver_pins_are_the_examples` keeps them equal."""
_DRIVER_REQUIREMENTS = """\
python-tds==1.17.1 \\
    --hash=sha256:35cb210b1a54e5ccc91570a83d4e9a2a16682cbeb00bede06fd6cdf9afa9762f
pyopenssl==26.4.0 \\
    --hash=sha256:f0eb0cb2d581d3ad2b9c489468485e7f2ab6727d08401bcf9d824c3caddf3c1c
cryptography==50.0.2 \\
    --hash=sha256:630ebfea3bf689d075f82316324ff7433dc447fe6bc1bfc76524b74b4a9567d2 \\
    --hash=sha256:4061c0079120205fb760c58acab6443e217307dcf05e3702cf970e0689972856 \\
    --hash=sha256:9dab55f57c74c3cad24c323bacbbd04be4705ba6eb0d92e920b1fc4837ed5079 \\
    --hash=sha256:ee247f5c245c9a2fe7c8e2214e295918838e44e00a45a6718451e4004219e767 \\
    --hash=sha256:f21e8a22c8605750c7af886bab299a363721264061b4ac0a30efb73cfd58efc5 \\
    --hash=sha256:1981f1db4630889b9ef7803fadef12b056f428cb6b85c27ba57b774793b6093c
cffi==2.1.1 \\
    --hash=sha256:34e261f78cb6ceaaa36f42f2613f4380d94d9c759a9c73c769ee6e0247364632 \\
    --hash=sha256:c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf \\
    --hash=sha256:a931079504ecc49efed7744c476a5c343a92fabf66dec2db95edb1b2fdc770e2 \\
    --hash=sha256:b0431303acaea1089ad4b3e9ce4e6518193def1118d4073ca848635ee4ea2e96
pycparser==3.12 \\
    --hash=sha256:a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e
"""
"""The driver and what it pulls in, with the SHA-256 of each linux x86-64 wheel PyPI has for it
(cryptography's abi3 wheels, cffi's for CPython 3.11 to 3.14): pip installs them with
``--require-hashes``, so a file added to a release later is refused. Another Python needs cffi's
hash for it, and the install says so."""

_CONFIG = b"""[network]
tlscert = /var/opt/mssql/tls/erp.pem
tlskey = /var/opt/mssql/tls/erp.key
tlsprotocols = 1.2
forceencryption = 1
"""
"""``mssql-conf``'s file: the module's certificate, TLS 1.2, and no session without TLS."""

_SETUP = """
CREATE TABLE dbo.orders (id bigint NOT NULL PRIMARY KEY, customer nvarchar(100) NOT NULL,
    amount money NOT NULL, ordered_at datetime2 NOT NULL, row_version rowversion);
INSERT INTO dbo.orders (id, customer, amount, ordered_at) VALUES
    (1, N'Åsa Berg', 1250.5, '2026-10-01T08:00:00.1234567'),
    (2, N'Kowalski', 99.9999, '2026-10-01T09:30:00'), (4, N'Müller GmbH', 0, '2026-10-02T10:00:00');
CREATE TABLE dbo.customers (id int NOT NULL PRIMARY KEY, customer_guid uniqueidentifier NOT NULL,
    name nvarchar(100) NOT NULL, country char(2) NOT NULL, updated_at datetimeoffset NOT NULL);
INSERT INTO dbo.customers VALUES
    (1, '6F9619FF-8B86-D011-B42D-00C04FC964FF', N'Åsa Berg', 'SE', '2026-10-01T08:00:00+02:00'),
    (2, '0E984725-C51C-4BF4-9960-E1C80E27ABA0', N'Kowalski', 'PL', '2026-10-02T12:30:00-04:00');
"""
"""The source tables; the page's ``reader.sql`` adds the login."""

_PROBE = """import importlib.util, json
from pathlib import Path

from lakana import asset


@asset(connections=["erp-db"])
def probe(ctx):
    spec = importlib.util.spec_from_file_location("recipe", Path(__file__).with_name({file!r}))
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)
    function = getattr(recipe, {function!r})
    table = function(ctx) if {with_context!r} else function()
    print("E2E " + json.dumps(
        {{"schema": [[f.name, str(f.type)] for f in table.schema], "rows": table.to_pylist()}},
        default=str,
    ), flush=True)
    raise RuntimeError("the probe is done")
"""
"""The task: the example's function, called as the harness would, and what it returned."""


def _password() -> str:
    """Generate a password SQL Server's policy accepts (three of four character classes).

    Returns
    -------
    str
        A new password.
    """
    return secrets.token_urlsafe(18) + "Aa1-"


@dataclass
class _Server:
    """The SQL Server container and what a case needs of it.

    Parameters
    ----------
    container
        The running container.
    address
        Its address on the sources' network.
    admin
        The ``sa`` password.
    reader
        The password of the read-only login ``lakana_reader``.
    """

    container: Any
    address: str
    admin: str
    reader: str

    def sql(self, query: str, database: str = "master") -> str:
        """Run SQL as ``sa`` inside the container.

        Parameters
        ----------
        query
            One batch.
        database
            The database it runs in.

        Returns
        -------
        str
            What sqlcmd printed.

        Raises
        ------
        AssertionError
            When sqlcmd fails.
        """
        code, out = self.container.exec_run(
            [_SQLCMD, "-S", "localhost", "-U", "sa", "-C", "-b", "-d", database, "-Q", query],
            environment={"SQLCMDPASSWORD": self.admin, "READER_PASSWORD": self.reader},
        )
        text = out.decode(errors="replace")
        assert code == 0, text
        return text


def _ready(server: _Server) -> None:
    """Wait up to 120 seconds for SQL Server to take a login.

    Parameters
    ----------
    server
        The server.

    Raises
    ------
    AssertionError
        When it does not.
    """
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            server.sql("SELECT 1")
        except AssertionError:
            time.sleep(1)
        else:
            return
    raise AssertionError(server.container.logs().decode(errors="replace")[-2000:])


@pytest.fixture(scope="module")
def erp(sources: Sources) -> Iterator[_Server]:
    """Start SQL Server on the sources' network with the module's certificate and the tables.

    Parameters
    ----------
    sources
        The module's stack.

    Yields
    ------
    _Server
        The server.
    """
    client = sources.stack.client
    if client.info().get("Architecture") != "x86_64":
        pytest.skip("Microsoft's SQL Server image is built for x86-64 only")
    _wheelhouse()  # every case needs it: skip (or fail in CI) before pulling the image
    directory = sources.material / f"erp-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    certificate, key = issue_leaf(sources.authority, directory, names=(_HOST,))
    admin = _password()
    # `create` never pulls: a fresh runner has no copy of the image (by digest) yet.
    client.images.pull(_IMAGE)
    container = client.containers.create(
        _IMAGE,
        environment={
            "ACCEPT_EULA": "Y",
            "MSSQL_SA_PASSWORD": admin,
            "MSSQL_MEMORY_LIMIT_MB": "2048",
        },
        mem_limit="3g",
        labels=sources.stack.labels,
        network=sources.network,
        networking_config={
            sources.network: client.api.create_endpoint_config(aliases=[_HOST, _OTHER_HOST])
        },
    )
    try:
        # The server runs as mssql (uid 10001), which must own its key.
        container.put_archive(
            "/var/opt/mssql",
            docker_stack._tar(
                [
                    ("mssql.conf", "file", _CONFIG, 0o644, 10001),
                    ("tls", "dir", b"", 0o700, 10001),
                    ("tls/erp.pem", "file", certificate.read_bytes(), 0o644, 10001),
                    ("tls/erp.key", "file", key.read_bytes(), 0o600, 10001),
                ]
            ),
        )
        container.start()
        server = _Server(container, _address(container, sources.network), admin, _password())
        _ready(server)
        server.sql("CREATE DATABASE erp")
        server.sql(_SETUP, database="erp")
        server.sql((_RECIPE.parent / "reader.sql").read_text())
        yield server
    finally:
        container.remove(force=True)


def _driver_environment(stack: Stack, wheels: Path) -> None:
    """Build the environment `_ENVIRONMENT_KEY`: the wheelhouse's lakana and the recipe's driver.

    ``lakana[assets]`` comes from the wheelhouse only; the driver and its tree from the public
    index, as the wheels `_DRIVER_REQUIREMENTS` names by hash, without resolving anything else.

    Parameters
    ----------
    stack
        The stack.
    wheels
        The wheel directory.
    """
    assert stack.launcher is not None
    volume = environment_volume(stack.launcher.settings, PYTHON, _ENVIRONMENT_KEY)
    marker = stack.root / "state" / "environments" / f"{volume}.complete"
    if marker.exists():
        return
    store = f"{stack.name}-erp-wheels"
    stack.client.volumes.create(store, labels=stack.labels)
    stack.client.volumes.create(volume, labels=stack.labels)
    stack.put(
        store,
        [
            *(
                (path.name, "file", path.read_bytes(), 0o644, 10002)
                for path in wheels.glob("*.whl")
            ),
            ("driver.txt", "file", _DRIVER_REQUIREMENTS.encode(), 0o644, 10002),
        ],
    )
    stack.client.containers.run(
        stack.image,
        [
            "sh",
            "-c",
            "python -m venv /env && /env/bin/python -m pip install -q --no-index "
            "--find-links /wheels 'lakana[assets]' && /env/bin/python -m pip install -q "
            "--require-hashes --no-deps --only-binary=:all: -r /wheels/driver.txt",
        ],
        user="10002:10002",
        environment={"HOME": "/tmp"},
        volumes={volume: {"bind": "/env", "mode": "rw"}, store: {"bind": "/wheels", "mode": "ro"}},
        labels=stack.labels,
        remove=True,
    )
    marker.write_text(volume)


def _run(
    sources: Sources,
    worker: _Mirrored,
    server: _Server,
    *,
    asset: str,
    address: dict[str, object] | None = None,
    password: str | None = None,
    watermark: str | None = None,
    trust: bytes | Literal["module"] | None = "module",
) -> tuple[dict[str, Any] | None, str, RunOutcome]:
    """Run one of the example's assets as a task with an ``erp-db`` tcp connection.

    Parameters
    ----------
    sources
        The module's stack.
    worker
        The worker's launcher client.
    server
        The server.
    asset
        ``erp_orders`` or ``erp_customers``: the file and its function.
    address
        The connection's address; `_HOST` on 1433 by default.
    password
        The secret's password; the reader's by default.
    watermark
        The encoded watermark the worker would send (``long:3``); `None` for a first run.
    trust
        The workers' trust bundle; the module's authority by default, `None` for none.

    Returns
    -------
    tuple
        What the probe printed (`None` when it printed nothing), the whole log, and the outcome.
    """
    _driver_environment(sources.stack, _wheelhouse())
    address = address or {"host": _HOST, "port": 1433}
    secret = {"user": "lakana_reader", "password": password or server.reader}
    declared = [("erp-db", ConnectionKind.TCP, address, {})]
    plan = connection_plan(declared, isolated=True, root_dir=lambda root, name: f"/ingest/{root}")
    _serve_connections(
        sources.stack,
        {
            "erp-db": {
                "name": "erp-db",
                "kind": "tcp",
                "address": address,
                "settings": {},
                "endpoints": [
                    {"host": e.host, "port": e.port, "role": e.role}
                    for e in endpoints(ConnectionKind.TCP, address, {})
                ],
                "secret": secret,
            }
        },
    )
    attempt = worker.root / f"attempt-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(mode=0o700)
    (attempt / "code").mkdir(mode=0o755)
    incremental = asset == "erp_orders"
    source = _RECIPE / "assets" / "bronze" / f"{asset}.py"
    (attempt / "code" / source.name).write_bytes(source.read_bytes())
    (attempt / "code" / "probe.py").write_text(
        _PROBE.format(file=source.name, function=asset, with_context=incremental)
    )
    launcher = sources.stack.launcher
    assert launcher is not None
    runner = DockerRunner(
        cast("LauncherClient", worker),
        owner="test",
        memory_mb=512,
        cpus=1.0,
        pids=128,
        kill_grace=2.0,
        trust=sources.bundle if trust == "module" else trust,
    )
    task = PythonTask(
        code_dir=attempt / "code",
        asset_path="probe.py",
        inputs={},
        table="bronze.erp",
        environment="dev",
        server_url=f"http://{launcher.settings.server_alias}:8000",
        token="t" * 48,
        timeout_seconds=120,
        venv=Path("/env"),
        expected_snapshot_id="none",
        attempt_dir=attempt,
        environment_key=_ENVIRONMENT_KEY,
        python=PYTHON,
        write="append" if incremental else "replace",
        watermark_column="id" if incremental else None,
        watermark=watermark,
        connections=plan.routing,
        egress=plan.egress,
        relay=plan.relay,
    )
    output = bytearray()
    reachable = (*launcher.settings.relay_reachable, f"{server.address}/32")
    with sources.stack.configured(relay_reachable=reachable):
        outcome = runner.run(task, output.extend, StopSignal())
    text = output.decode(errors="replace")
    reports = [line for line in text.splitlines() if line.startswith("E2E ")]
    assert outcome.succeeded is False, text
    assert server.reader not in text
    assert not [name for name in sources.stack.labelled() if "task" in name]
    return (json.loads(reports[-1].removeprefix("E2E ")) if reports else None), text, outcome


def test_the_driver_pins_are_the_examples() -> None:
    """The environment of this module has the driver the page tells a team to install."""
    project = tomllib.loads((_RECIPE / "pyproject.toml").read_text())["project"]
    assert [pin for pin in project["dependencies"] if not pin.startswith("lakana")] == list(_DRIVER)
    pinned = [line.split()[0] for line in _DRIVER_REQUIREMENTS.splitlines() if "==" in line]
    assert pinned[: len(_DRIVER)] == list(_DRIVER)


def test_the_full_read_returns_every_customer_with_its_types(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
) -> None:
    """``erp_customers``: TLS through the relay, the name checked, every row, the page's types.

    The certificate is checked for `_HOST`, the connection's host, although the socket goes to
    the relay; a ``uniqueidentifier`` becomes its text and a ``datetimeoffset`` UTC.
    """
    seen, text, _ = _run(sources, worker_side, erp, asset="erp_customers")

    assert seen == {
        "schema": [
            ["id", "int32"],
            ["customer_guid", "string"],
            ["name", "string"],
            ["country", "string"],
            ["updated_at", "timestamp[us, tz=UTC]"],
        ],
        "rows": [
            {
                "id": 1,
                "customer_guid": "6f9619ff-8b86-d011-b42d-00c04fc964ff",
                "name": "Åsa Berg",
                "country": "SE",
                "updated_at": "2026-10-01 06:00:00+00:00",
            },
            {
                "id": 2,
                "customer_guid": "0e984725-c51c-4bf4-9960-e1c80e27aba0",
                "name": "Kowalski",
                "country": "PL",
                "updated_at": "2026-10-02 16:30:00+00:00",
            },
        ],
    }, text


def test_the_incremental_read_finds_a_late_row_and_a_changed_one(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
) -> None:
    """``erp_orders`` by ``rowversion``: the next run reads what was written after the first.

    Order 3 arrives after the first run read 1, 2 and 4, as a row does whose id was taken before
    the run but committed after it: by id it is behind the largest one read, by rowversion
    after it. Order 1 changes. The second run, from the first's largest ``row_version``, reads
    exactly those two; ``money`` stays exact as ``decimal128(19, 4)``, ``datetime2``'s seventh
    digit is cut.
    """
    schema = [
        ["id", "int64"],
        ["customer", "string"],
        ["amount", "decimal128(19, 4)"],
        ["ordered_at", "timestamp[us]"],
        ["row_version", "int64"],
    ]

    first, text, _ = _run(sources, worker_side, erp, asset="erp_orders")
    assert first is not None, text
    watermark = max(row["row_version"] for row in first["rows"])
    erp.sql(
        "INSERT INTO dbo.orders (id, customer, amount, ordered_at) "
        "VALUES (3, N'Ngata', 18.75, '2026-10-03T07:15:00'); "
        "UPDATE dbo.orders SET amount = 1300 WHERE id = 1",
        database="erp",
    )
    second, later, _ = _run(
        sources, worker_side, erp, asset="erp_orders", watermark=f"long:{watermark}"
    )
    assert second is not None, later

    assert first["schema"] == schema, text
    assert [{k: v for k, v in row.items() if k != "row_version"} for row in first["rows"]] == [
        {
            "id": 1,
            "customer": "Åsa Berg",
            "amount": "1250.5000",
            "ordered_at": "2026-10-01 08:00:00.123456",
        },
        {"id": 2, "customer": "Kowalski", "amount": "99.9999", "ordered_at": "2026-10-01 09:30:00"},
        {
            "id": 4,
            "customer": "Müller GmbH",
            "amount": "0.0000",
            "ordered_at": "2026-10-02 10:00:00",
        },
    ], text
    assert second["schema"] == schema, later
    assert [(row["id"], row["amount"]) for row in second["rows"]] == [
        (3, "18.7500"),
        (1, "1300.0000"),
    ], later
    assert min(row["row_version"] for row in second["rows"]) > watermark


def test_a_refused_login_names_the_connection_and_never_the_password(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
) -> None:
    """A wrong password: the recipe's message names the connection and what to do."""
    wrong = _password()

    seen, text, _ = _run(sources, worker_side, erp, asset="erp_customers", password=wrong)

    assert seen is None
    assert (
        "RuntimeError: erp-db: the server refused the login (Login failed for user '***'.); "
        "check the user and the password: lakana connection update erp-db --values-stdin"
    ) in text
    assert wrong not in text


def test_an_unreachable_port_leaves_the_relay_line_in_the_log(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
) -> None:
    """Nothing listens on 1434: the relay says why in the task's log, before the driver's error."""
    address: dict[str, object] = {"host": _HOST, "port": 1434}

    seen, text, _ = _run(sources, worker_side, erp, asset="erp_customers", address=address)

    assert seen is None
    lines = text.splitlines()
    relay = f"lakana-relay: refused {_HOST}:1434: cannot reach {_HOST}:1434 (Connection refused)"
    recipe = (
        "RuntimeError: erp-db: no answer from the server; under the Docker runner a line "
        "'lakana-relay: refused ...' above says why; check the address: "
        "lakana connection test erp-db"
    )
    assert relay in lines, text
    assert recipe in lines, text
    assert lines.index(relay) < lines.index(recipe)


def test_a_certificate_for_another_name_is_refused_through_the_relay(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
) -> None:
    """The same server by a name its certificate does not hold: no login, no rows.

    python-tds 1.17.1 compares the name with the certificate's common name first; for any other
    name it reads the subject alternative names with an API pyOpenSSL 26.2 removed, so the
    refusal is an `AttributeError` until a release has the fix (the page says so).
    """
    address: dict[str, object] = {"host": _OTHER_HOST, "port": 1433}

    seen, text, _ = _run(sources, worker_side, erp, asset="erp_customers", address=address)

    assert seen is None
    assert "AttributeError: 'X509' object has no attribute 'get_extension'" in text, text


def test_without_a_trust_bundle_the_recipe_sends_no_password(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
) -> None:
    """No ``LAKANA_RUN__CA_BUNDLE``: pytds would log in unencrypted, so the recipe refuses."""
    seen, text, _ = _run(sources, worker_side, erp, asset="erp_customers", trust=None)

    assert seen is None
    assert (
        "RuntimeError: erp-db: no CA to check the server's certificate; set "
        "LAKANA_RUN__CA_BUNDLE on the worker to the CA that signed it"
    ) in text.splitlines(), text


def test_a_certificate_of_another_authority_is_refused(
    sources: Sources,
    worker_side: _Mirrored,
    erp: _Server,
    tmp_path: Path,
) -> None:
    """A trust bundle without the server's authority: the chain fails, no login is sent."""
    other = build_trust_bundle(make_authority(tmp_path, "Another Test CA").ca_file)

    seen, text, _ = _run(sources, worker_side, erp, asset="erp_customers", trust=other)

    assert seen is None
    assert "certificate verify failed" in text, text
