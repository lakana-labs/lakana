"""OIDC sign-in against a real Keycloak in a container (#32).

Keycloak (`IMAGE`, pinned by tag and digest; `docs/agent/testing.md`, "Identity provider
images") runs with the realm `keycloak/lakana-realm.json`: the client `lakana` with the settings
the how-to "Sign in through Keycloak" shows (`scripts/tests/test_oidc_how_tos.py` keeps the two
equal), a second client `other`, and the people `kim`, `ola`, `leaver` and `disabled-person` (the
last one disabled). It serves HTTPS with a leaf of a test authority for ``keycloak.test`` on
port 8443 of the container's bridge address, and names that port in its issuer, as the server's
port rule allows (the issuer's own port, besides 443).

The server under test is the in-process app. Its guarded fetcher reaches Keycloak because
`LAKANA_OIDC__ALLOW_HOSTS` names ``keycloak.test`` (a private address), the resolver
(`lakana_core.net.https_json._resolve`) answers the container's address for that name, and
`LAKANA_OIDC__CA_BUNDLE` is the test authority. The browser is `KeycloakBrowser`: it signs in at
Keycloak's own login form and brings Keycloak's redirect back to the app's callback.

Linux only, like the other container tests: the test process must reach the container's bridge
address directly.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

import lakana
import lakana_server.ui
from lakana._models import DeviceSignIn
from lakana.cli._state import CliState
from lakana_core.models import AuditEvent, AuditEventType, CliGrant, Passkey, Principal
from lakana_core.net import https_json
from lakana_core.oidc.config import OidcSignInConfig
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.cli.conftest import run_cli
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.keycloak_browser import KeycloakBrowser
from tests.server_test_helpers import session_headers

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux", reason="needs the container's bridge address (Linux only)"
    ),
]

IMAGE = (
    "quay.io/keycloak/keycloak:26.8.2"
    "@sha256:7e66a96e4739239070aeaf72e4c5decf0f7f682cfc725b9f7ea2b336eaba38b3"
)
HOST = "keycloak.test"
PORT = 8443
ISSUER = f"https://{HOST}:{PORT}/realms/lakana"
REALM = Path(__file__).parent / "keycloak" / "lakana-realm.json"
CLIENT_SECRET = "lakana-client-secret"  # nosec B105 -- the realm file's test value
ADMIN_PASSWORD = "keycloak-admin-test"  # nosec B105 -- the container's own admin, a test value
START = "/api/v1/sign-in/oidc/start"
CALLBACK = "/api/v1/sign-in/oidc/callback"
FINISH = "/api/v1/sign-in/oidc/finish"
SESSION_COOKIE = "lakana_session_8000"


@dataclass(frozen=True)
class Keycloak:
    """The running Keycloak.

    Attributes
    ----------
    address
        The container's bridge address.
    ca_file
        The test authority's certificate.
    container
        The container.
    """

    address: str
    ca_file: Path
    container: DockerContainer


def _pem(key: rsa.RSAPrivateKey) -> bytes:
    """Write a private key as unencrypted PKCS #8 PEM.

    Parameters
    ----------
    key
        The key.

    Returns
    -------
    bytes
        The PEM text.
    """
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _certificates(directory: Path) -> tuple[Path, bytes, bytes]:
    """Make a test authority and a leaf for `HOST`, valid for two days.

    Parameters
    ----------
    directory
        Where the authority's certificate is written.

    Returns
    -------
    tuple of (Path, bytes, bytes)
        The authority's certificate file, the leaf certificate and the leaf's key, both PEM.
    """
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lakana Keycloak Test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # Python checks strictly (VERIFY_X509_STRICT): a leaf names its issuer's key.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = directory / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return ca_file, leaf.public_bytes(serialization.Encoding.PEM), _pem(leaf_key)


@pytest.fixture(scope="module")
def keycloak(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Keycloak]:
    """Start Keycloak with the realm imported, over TLS for `HOST`.

    Parameters
    ----------
    tmp_path_factory
        Pytest's temp directory factory.

    Yields
    ------
    Keycloak
        The running Keycloak; removed afterwards.
    """
    ca_file, leaf, key = _certificates(tmp_path_factory.mktemp("keycloak"))
    container = (
        DockerContainer(
            IMAGE,
            _wait_strategy=LogMessageWaitStrategy(r"started in").with_startup_timeout(240),
        )
        .with_command("start-dev --import-realm")
        .with_env("KC_HOSTNAME", f"https://{HOST}:{PORT}")
        .with_env("KC_HTTPS_PORT", str(PORT))
        .with_env("KC_HTTPS_CERTIFICATE_FILE", "/opt/keycloak/conf/tls.crt")
        .with_env("KC_HTTPS_CERTIFICATE_KEY_FILE", "/opt/keycloak/conf/tls.key")
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", ADMIN_PASSWORD)
        .with_copy_into_container(leaf, "/opt/keycloak/conf/tls.crt")
        .with_copy_into_container(key, "/opt/keycloak/conf/tls.key")
        .with_copy_into_container(REALM, "/opt/keycloak/data/import/lakana-realm.json")
    )
    with container:
        wrapped = container.get_wrapped_container()
        wrapped.reload()
        # Docker 29 names the address only per network; the container is on the default one.
        attrs = wrapped.attrs
        assert attrs is not None
        [network] = attrs["NetworkSettings"]["Networks"].values()
        address = network["IPAddress"]
        yield Keycloak(address=address, ca_file=ca_file, container=container)


@pytest.fixture
def browser(keycloak: Keycloak) -> Iterator[KeycloakBrowser]:
    """Provide a new browser for Keycloak, with no cookies yet.

    Parameters
    ----------
    keycloak
        The running Keycloak.

    Yields
    ------
    KeycloakBrowser
        The browser.
    """
    opened = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
    try:
        yield opened
    finally:
        opened.close()


def _app_client(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keycloak: Keycloak,
    *,
    client_secret: str = CLIENT_SECRET,
) -> TestClient:
    """Build the app with OIDC sign-in through the container's Keycloak.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build of the interface.
    monkeypatch
        Points the interface at the stand-in and `HOST` at the container.
    keycloak
        The running Keycloak.
    client_secret
        The client secret the server is given.

    Returns
    -------
    TestClient
        A client at the public address, not started.
    """
    real = https_json._resolve

    def resolve(host: str, port: int) -> list[str]:
        return [keycloak.address] if host == HOST else real(host, port)

    monkeypatch.setattr(https_json, "_resolve", resolve)
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    oidc = OidcSignInConfig(
        issuer=ISSUER,
        client_id="lakana",
        client_secret=client_secret,
        display_name="Keycloak",
        allow_hosts=[HOST],
        ca_bundle=keycloak.ca_file,
    )
    app = create_app(
        settings.model_copy(update={"ui": UiConfig(enabled=True, public_url=PUBLIC), "oidc": oidc})
    )
    return TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC})


@pytest.fixture
def client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keycloak: Keycloak
) -> Iterator[TestClient]:
    """Provide the browser's side at the app, which signs people in through Keycloak.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build of the interface.
    monkeypatch
        See `_app_client`.
    keycloak
        The running Keycloak.

    Yields
    ------
    TestClient
        A started client.
    """
    with _app_client(settings, tmp_path, monkeypatch, keycloak) as c:
        yield c


# -- helpers -----------------------------------------------------------------------------------


def _come_back(client: TestClient, location: httpx.Response | str) -> None:
    """Bring Keycloak's redirect to the app's callback, as the browser does.

    Parameters
    ----------
    client
        The app's client, holding the flow cookie.
    location
        Where Keycloak sent the browser.
    """
    assert isinstance(location, str), f"Keycloak did not send the browser back: {location}"
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == PUBLIC + CALLBACK, location
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    answer = client.get(CALLBACK, params=params, follow_redirects=False)
    assert answer.status_code == 303, answer.text


def _signed_in(
    client: TestClient, browser: KeycloakBrowser, user: str, body: dict[str, str] | None = None
) -> httpx.Response:
    """Start a flow at the app, sign in at Keycloak, come back and finish.

    Parameters
    ----------
    client
        The app's client.
    browser
        The browser at Keycloak.
    user
        Who signs in at Keycloak; the password is the realm file's.
    body
        The start's body (an invite's halves); a plain sign-in without.

    Returns
    -------
    httpx.Response
        The finish's answer.
    """
    started = client.post(START, json=body or {})
    assert started.status_code == 200, started.text
    _come_back(
        client,
        browser.sign_in(started.json()["authorization_url"], user, f"{user}-password-test"),
    )
    return client.post(FINISH, json={})


def _refused(answer: httpx.Response, reason: str) -> None:
    """Check that a finish signed nobody in, for the reason given.

    Parameters
    ----------
    answer
        The finish's answer.
    reason
        The expected reason.
    """
    assert answer.status_code == 403, answer.text
    assert (answer.json()["type"], answer.json()["reason"]) == ("OidcSignInRefused", reason)
    assert SESSION_COOKIE not in answer.headers.get("set-cookie", "")


def _invite(client: TestClient, name: str) -> dict[str, str]:
    """Issue an invite as the server operator.

    Parameters
    ----------
    client
        The app's client.
    name
        The invited name.

    Returns
    -------
    dict of str to str
        The start's body for the invite.
    """
    made = client.post("/api/v1/invites", headers=AUTH_HEADER, json={"name": name})
    assert made.status_code == 201, made.text
    values = dict(item.split("=", 1) for item in made.json()["url"].split("#", 1)[1].split("&"))
    return {"purpose": "invite", "link_id": values["l"], "secret": values["s"]}


def _joined(client: TestClient, browser: KeycloakBrowser, name: str) -> dict[str, str]:
    """Invite a person and have them join through Keycloak.

    Parameters
    ----------
    client
        The app's client.
    browser
        The browser at Keycloak.
    name
        The person, who is also their Keycloak user name.

    Returns
    -------
    dict of str to str
        The browser session's headers (cookie and CSRF token); the client keeps no cookie.
    """
    finished = _signed_in(client, browser, name, _invite(client, name))
    assert finished.status_code == 200, finished.text
    assert finished.json()["name"] == name
    value = client.cookies.get(SESSION_COOKIE) or ""
    client.cookies.clear()
    return session_headers(SESSION_COOKIE, value, finished.json()["csrf_token"])


def _principal(client: TestClient, name: str) -> Principal:
    """Read a principal's row.

    Parameters
    ----------
    client
        The app's client.
    name
        The name.

    Returns
    -------
    Principal
        The row, detached.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine, expire_on_commit=False) as session:
        return session.execute(select(Principal).where(Principal.name == name)).scalar_one()


def _events(client: TestClient, kind: AuditEventType) -> list[dict[str, Any]]:
    """Read the details of the audit events of one type, oldest first.

    Parameters
    ----------
    client
        The app's client.
    kind
        The type.

    Returns
    -------
    list of dict
        The details.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        rows = session.execute(
            select(AuditEvent.detail)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
        return [dict(row) for row in rows]


def _admin_api(browser: KeycloakBrowser) -> dict[str, str]:
    """Sign in to Keycloak's admin REST API as its bootstrap administrator.

    Parameters
    ----------
    browser
        The browser at Keycloak.

    Returns
    -------
    dict of str to str
        The ``Authorization`` header.
    """
    answer = browser.request(
        "POST",
        f"https://{HOST}:{PORT}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": ADMIN_PASSWORD,
        },
    )
    assert answer.status_code == 200, answer.text
    return {"Authorization": f"Bearer {answer.json()['access_token']}"}


ADMIN_REALM = f"https://{HOST}:{PORT}/admin/realms/lakana"


def _set_enabled(browser: KeycloakBrowser, username: str, *, enabled: bool) -> None:
    """Enable or disable a Keycloak user, as its administrator does.

    Parameters
    ----------
    browser
        The browser at Keycloak.
    username
        The user.
    enabled
        Whether the user may sign in.
    """
    admin = _admin_api(browser)
    found = browser.request(
        "GET", f"{ADMIN_REALM}/users?username={username}&exact=true", headers=admin
    )
    [user] = found.json()
    changed = browser.request(
        "PUT", f"{ADMIN_REALM}/users/{user['id']}", json={"enabled": enabled}, headers=admin
    )
    assert changed.status_code == 204, changed.text


def _rotate_the_realm_key(browser: KeycloakBrowser) -> None:
    """Make a new RSA signing key active and disable every older one.

    Parameters
    ----------
    browser
        The browser at Keycloak.
    """
    admin = _admin_api(browser)
    kind = "org.keycloak.keys.KeyProvider"
    listed = browser.request("GET", f"{ADMIN_REALM}/components?type={kind}", headers=admin)
    older = [c for c in listed.json() if c["providerId"] == "rsa-generated"]
    assert older, listed.text
    added = browser.request(
        "POST",
        f"{ADMIN_REALM}/components",
        json={
            "name": f"rsa-rotated-{uuid.uuid4().hex[:8]}",
            "providerId": "rsa-generated",
            "providerType": kind,
            "parentId": "lakana",
            "config": {"priority": ["1000"], "enabled": ["true"], "active": ["true"]},
        },
        headers=admin,
    )
    assert added.status_code == 201, added.text
    for component in older:
        component["config"]["enabled"] = ["false"]
        component["config"]["active"] = ["false"]
        put = browser.request(
            "PUT", f"{ADMIN_REALM}/components/{component['id']}", json=component, headers=admin
        )
        assert put.status_code == 204, put.text


def _unlinked_administrator_with_a_passkey(client: TestClient, name: str) -> None:
    """Create an administrator who is not linked and has a passkey: the break-glass account.

    Parameters
    ----------
    client
        The app's client.
    name
        The name.
    """
    body = {"name": name, "type": "USER"}
    assert client.post("/api/v1/principals", headers=AUTH_HEADER, json=body).status_code == 201
    grant = {"principal": name, "role": "ADMIN", "kind": "INSTANCE"}
    assert client.post("/api/v1/grants", headers=AUTH_HEADER, json=grant).status_code == 201
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        person = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
        session.add(
            Passkey(
                id=uuid.uuid4(),
                principal_id=person.id,
                credential_id=uuid.uuid4().hex,
                public_key=b"k",
                sign_count=0,
                rp_id="localhost",
                name="Passkey",
                backup_eligible=False,
                created_at=datetime.now(UTC),
                created_user_agent="pytest",
            )
        )
        session.commit()


# -- sign-in and linking -----------------------------------------------------------------------


def test_invite_then_sign_in_through_keycloak(
    client: TestClient, browser: KeycloakBrowser, keycloak: Keycloak
) -> None:
    headers = _joined(client, browser, "kim")
    kim = _principal(client, "kim")
    [linked] = _events(client, AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert (kim.oidc_issuer, kim.oidc_subject) == (ISSUER, linked["sub"])
    assert kim.oidc_email == "kim@example.com"
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        passkeys = session.execute(select(Passkey).where(Passkey.principal_id == kim.id)).all()
    assert passkeys == []
    current = client.get("/api/v1/sign-in/session", headers=headers)
    assert (current.json()["name"], current.json()["via"]) == ("kim", "oidc")
    # A second sign-in, from a new browser, finds kim by Keycloak's subject.
    again = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
    try:
        finished = _signed_in(client, again, "kim")
    finally:
        again.close()
    assert finished.status_code == 200, finished.text
    assert finished.json()["name"] == "kim"
    events = _events(client, AuditEventType.SIGN_IN_OIDC)
    assert [(e["iss"], e["sub"]) for e in events] == [(ISSUER, linked["sub"])] * 2


def test_unlinked_identity_is_not_linked(client: TestClient, browser: KeycloakBrowser) -> None:
    _refused(_signed_in(client, browser, "ola"), "not_linked")
    [event] = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert (event["reason"], event["iss"]) == ("not_linked", ISSUER)


def test_disabled_person_cannot_sign_in(client: TestClient, browser: KeycloakBrowser) -> None:
    started = client.post(START, json={})
    page = browser.sign_in(started.json()["authorization_url"], "disabled-person", "x")
    # Keycloak answers with its own page and never sends the browser back.
    assert isinstance(page, httpx.Response), page
    assert page.status_code == 200
    answer = client.post(FINISH, json={})
    assert answer.status_code == 403, answer.text
    assert SESSION_COOKIE not in answer.headers.get("set-cookie", "")
    assert _events(client, AuditEventType.SIGN_IN_OIDC) == []


def test_token_stops_after_the_window_for_a_disabled_person(
    client: TestClient, browser: KeycloakBrowser, keycloak: Keycloak
) -> None:
    _joined(client, browser, "leaver")
    issued = client.post(
        "/api/v1/principals/leaver/tokens", headers=AUTH_HEADER, json={"name": "laptop"}
    )
    token = {"Authorization": f"Bearer {issued.json()['token']}"}
    admin_browser = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
    _set_enabled(admin_browser, "leaver", enabled=False)
    try:
        # Disabled at Keycloak: no new sign-in, but the token works within the window.
        started = client.post(START, json={})
        retry = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
        try:
            page = retry.sign_in(
                started.json()["authorization_url"], "leaver", "leaver-password-test"
            )
        finally:
            retry.close()
        assert isinstance(page, httpx.Response), page
        assert client.get("/api/v1/whoami", headers=token).status_code == 200
        # 31 days after the last sign-in at Keycloak the window (30 days) is over.
        app = cast(FastAPI, client.app)
        with Session(app.state.engine) as session:
            session.execute(
                update(Principal)
                .where(Principal.name == "leaver")
                .values(last_provider_sign_in_at=datetime.now(UTC) - timedelta(days=31))
            )
            session.commit()
        refused = client.get("/api/v1/whoami", headers=token)
        assert refused.status_code == 401, refused.text
        assert "Sign in to Lakana in the browser once" in refused.json()["detail"]
        listed = client.get("/api/v1/principals/leaver/tokens", headers=AUTH_HEADER).json()
        assert [row["revoked_at"] for row in listed] == [None]
    finally:
        _set_enabled(admin_browser, "leaver", enabled=True)
        admin_browser.close()


def test_sign_in_survives_a_key_rotation(
    client: TestClient, browser: KeycloakBrowser, keycloak: Keycloak
) -> None:
    _joined(client, browser, "kim")
    admin_browser = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
    try:
        _rotate_the_realm_key(admin_browser)
    finally:
        admin_browser.close()
    keys = cast(FastAPI, client.app).state.oidc_provider._keys
    real = keys._clock
    # Within a minute of the last fetch the cache does not fetch again for an unknown key id.
    fresh = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
    try:
        _refused(_signed_in(client, fresh, "kim"), "token_invalid")
    finally:
        fresh.close()
    # A minute later it fetches the keys once more, and the new key signs kim in.
    keys._clock = lambda: real() + 61
    later = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file)
    try:
        finished = _signed_in(client, later, "kim")
    finally:
        later.close()
    assert finished.status_code == 200, finished.text


def test_a_code_issued_to_another_client_is_refused(
    client: TestClient, browser: KeycloakBrowser
) -> None:
    _joined(client, browser, "kim")
    started = client.post(START, json={})
    parts = urlsplit(started.json()["authorization_url"])
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    query["client_id"] = "other"
    elsewhere = urlunsplit(parts._replace(query=urlencode(query)))
    fresh = KeycloakBrowser(browser.address, HOST, browser.ca_file)
    try:
        _come_back(client, fresh.sign_in(elsewhere, "kim", "kim-password-test"))
    finally:
        fresh.close()
    _refused(client.post(FINISH, json={}), "provider_refused")


def test_wrong_client_secret_is_provider_refused(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keycloak: Keycloak,
    browser: KeycloakBrowser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    wrong = "not-the-client-secret"  # nosec B105 -- a test value
    with _app_client(settings, tmp_path, monkeypatch, keycloak, client_secret=wrong) as client:
        _refused(_signed_in(client, browser, "ola"), "provider_refused")
    assert wrong not in caplog.text


# -- step-up, the command line and break-glass -------------------------------------------------


def test_provider_step_up_and_lakana_login_device(
    client: TestClient, browser: KeycloakBrowser, tmp_path: Path
) -> None:
    headers = _joined(client, browser, "kim")
    csrf = {"X-Lakana-CSRF": headers["X-Lakana-CSRF"]}
    client.cookies.set(SESSION_COOKIE, headers["Cookie"].split("=", 1)[1])
    started = client.post("/api/v1/sign-in/oidc/step-up/start", headers=csrf, json={})
    assert started.status_code == 200, started.text
    url = started.json()["authorization_url"]
    assert {"prompt": ["login"], "max_age": ["0"]}.items() <= parse_qs(urlsplit(url).query).items()
    # The browser holds Keycloak's session from joining, and Keycloak still asks for a sign-in.
    _come_back(client, browser.sign_in(url, "kim", "kim-password-test"))
    confirmed = client.post("/api/v1/sign-in/oidc/finish-in-session", headers=csrf, json={})
    assert confirmed.status_code == 200, confirmed.text
    client.cookies.clear()
    assert client.get("/api/v1/sign-in/session", headers=headers).json()["step_up_until"]

    # `lakana login --device`: the code is confirmed in kim's session.
    started_devices: list[DeviceSignIn] = []

    def factory(url: str, token: str) -> lakana.Client:
        built = lakana.Client(url=url, token=token, http_client=client)
        start = built.sign_in.start_device

        def recorded(host: str) -> DeviceSignIn:
            started_devices.append(start(host))
            return started_devices[-1]

        built.sign_in.start_device = recorded  # type: ignore[method-assign]
        return built

    def approve(seconds: float) -> None:
        app = cast(FastAPI, client.app)
        with Session(app.state.engine) as session:
            session.execute(update(CliGrant).values(last_polled_at=None))
            session.commit()
            waiting = session.execute(select(CliGrant.approved_at)).scalar_one()
        if waiting is None:
            code = {"user_code": started_devices[0].user_code}
            approved = client.post(
                "/api/v1/sign-in/cli/device/approve",
                headers={**headers, "Origin": PUBLIC},
                json=code,
            )
            assert approved.status_code == 204, approved.text

    state = CliState(
        login_client_factory=factory,
        is_interactive=lambda: False,
        config_path=tmp_path / "lakana" / "config.toml",
        hostname=lambda: "laptop-7",
        login_timeout=10.0,
        sleep=approve,
    )
    result = run_cli(state, "login", "--url", PUBLIC, "--device")
    assert result.exit_code == 0, result.stderr
    saved = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]["token"]
    who = client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {saved}"})
    assert who.json()["name"] == "kim"
    assert who.json()["token_window_ends_at"] is not None


def test_operator_recovery_unlinks_with_keycloak_down(
    client: TestClient, browser: KeycloakBrowser, keycloak: Keycloak
) -> None:
    _unlinked_administrator_with_a_passkey(client, "breakglass")
    _joined(client, browser, "kim")
    grant = {"principal": "kim", "role": "ADMIN", "kind": "INSTANCE"}
    assert client.post("/api/v1/grants", headers=AUTH_HEADER, json=grant).status_code == 201
    wrapped = keycloak.container.get_wrapped_container()
    wrapped.pause()
    try:
        probe = KeycloakBrowser(keycloak.address, HOST, keycloak.ca_file, timeout=2.0)
        try:
            with pytest.raises(httpx.TimeoutException):
                probe.request("GET", f"{ISSUER}/.well-known/openid-configuration")
        finally:
            probe.close()
        # The server operator's recovery link (the bootstrap token), used with Keycloak down.
        made = client.post("/api/v1/principals/kim/recovery-link", headers=AUTH_HEADER)
        assert made.status_code == 201, made.text
        values = dict(item.split("=", 1) for item in made.json()["url"].split("#", 1)[1].split("&"))
        used = client.post(
            "/api/v1/sign-in/links/redeem", json={"link_id": values["l"], "secret": values["s"]}
        )
        assert used.status_code == 200, used.text
    finally:
        wrapped.unpause()
    kim = _principal(client, "kim")
    assert (kim.oidc_issuer, kim.oidc_subject) == (None, None)
    [unlinked] = _events(client, AuditEventType.PRINCIPAL_OIDC_UNLINKED)
    assert unlinked["reason"] == "operator_recovery"
