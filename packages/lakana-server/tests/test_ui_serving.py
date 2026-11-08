"""Tests for serving the web interface (`lakana_server.ui`, `LAKANA_UI__ENABLED`)."""

from __future__ import annotations

import asyncio
import logging
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message

import lakana_server.ui
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from lakana_server.ui import CSP_FILE, UiNotBuiltError, reserved_segments
from tests.conftest import AUTH_HEADER

MARKER = "<!-- fixture-index -->"
"""In the fixture's `index.html` only, so a real local build can never stand in for it."""

SECRET = "never-served-secret"
"""The text of a file next to the build, which no request may read."""

INDEX = (
    "<!doctype html><html><head>"
    '<script src="/theme-boot.js"></script>'
    '<script type="module" src="/assets/app-1a2b.js"></script>'
    f"</head><body>{MARKER}<div id=root></div></body></html>"
)

POLICY = "default-src 'self'; script-src 'self'; frame-ancestors 'none'; report-to fixture"
"""The fixture build's policy: the server must send exactly what the build wrote."""

UI_HEADERS = {
    "content-security-policy": POLICY,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


@pytest.fixture
def ui_dist(tmp_path: Path) -> Path:
    """Write a small stand-in for the built interface, with a secret file beside it.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.

    Returns
    -------
    Path
        The stand-in build directory.
    """
    dist = tmp_path / "ui_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "licenses").mkdir()
    (dist / "index.html").write_text(INDEX)
    (dist / CSP_FILE).write_text(POLICY)
    (dist / "assets" / "app-1a2b.js").write_text("console.log('app');")
    (dist / "assets" / "plex-9z8y.woff2").write_bytes(b"wOF2font")
    (dist / "theme-boot.js").write_text("document.documentElement.dataset.theme='light';")
    (dist / "licenses" / "IBM-Plex-OFL-1.1.txt").write_text("SIL Open Font License 1.1")
    (dist / "licenses" / "THIRD-PARTY-NOTICES.md").write_text("# Licenses\n\n## react - 19 (MIT)\n")
    (tmp_path / "secret.txt").write_text(SECRET)
    (tmp_path / "ui.py").write_text(SECRET)
    (tmp_path / "settings.py").write_text(SECRET)
    return dist


def _ui_client(settings: Settings, monkeypatch: pytest.MonkeyPatch, dist: Path) -> TestClient:
    """Build an app that serves `dist` as its interface.

    Parameters
    ----------
    settings
        The test settings, turned on for the interface here.
    monkeypatch
        Points `lakana_server.ui.UI_DIST` at `dist`.
    dist
        The build directory to serve.

    Returns
    -------
    TestClient
        A client for the app, not yet started.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", dist)
    return TestClient(create_app(settings.model_copy(update={"ui": UiConfig(enabled=True)})))


@pytest.fixture
def ui_client(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a client for an app that serves the fixture's interface.

    Parameters
    ----------
    ui_dist
        The stand-in build directory.
    settings
        The test settings.
    monkeypatch
        Points `lakana_server.ui.UI_DIST` at the stand-in.

    Yields
    ------
    TestClient
        A client for the running app, checked to serve the fixture and not a real build.
    """
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        assert MARKER in c.get("/").text
        yield c


def _assert_ui_headers(response: httpx.Response) -> None:
    """Check that a response carries every header of a page of the interface.

    Parameters
    ----------
    response
        The response to check.
    """
    for name, value in UI_HEADERS.items():
        assert response.headers.get(name) == value, name


def test_the_interface_is_off_by_default(settings: Settings, client: TestClient) -> None:
    """Without `LAKANA_UI__ENABLED` the server serves no page at `/`."""
    assert UiConfig().enabled is False
    assert settings.ui.enabled is False
    assert client.get("/").status_code == 404


def test_the_start_up_line_names_the_interface(
    ui_dist: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Turned on, the server says once where the interface is."""
    with caplog.at_level(logging.INFO, logger="lakana_server.ui"):
        _ui_client(settings, monkeypatch, ui_dist)
    lines = [r for r in caplog.records if r.name == "lakana_server.ui"]
    assert [(r.levelno, r.getMessage()) for r in lines] == [
        (logging.INFO, "The web interface is served at /.")
    ]


def test_no_start_up_line_when_the_interface_is_off(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Turned off, nothing about the interface is logged."""
    with caplog.at_level(logging.DEBUG):
        create_app(settings)
    assert not [r for r in caplog.records if "web interface" in r.getMessage()]


def test_root_serves_the_index_with_its_headers(ui_client: TestClient) -> None:
    """`/` is the interface's page, with the strict headers and no caching."""
    response = ui_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == INDEX
    _assert_ui_headers(response)


@pytest.mark.parametrize(
    "path",
    [
        "/catalog",
        "/catalog/sales.orders",
        "/settings/status",
        "/no/such/page",
        "/missing.png",
        "/assets",
        "/licenses",
        "/apis",
        "/v1x/catalog",
    ],
)
def test_every_app_path_serves_the_index(ui_client: TestClient, path: str) -> None:
    """A deep link, dots included (table names have them), loads the interface's page."""
    response = ui_client.get(path)
    assert response.status_code == 200
    assert MARKER in response.text
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    _assert_ui_headers(response)


def test_head_on_the_index(ui_client: TestClient) -> None:
    """HEAD answers like GET, without a body."""
    for path in ("/", "/catalog/sales.orders"):
        response = ui_client.head(path)
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        _assert_ui_headers(response)


def test_another_method_on_a_page_is_refused(ui_client: TestClient) -> None:
    """The interface's pages are read-only: a POST is 405, never the page."""
    response = ui_client.post("/catalog")
    assert response.status_code == 405
    assert MARKER not in response.text


def test_a_hashed_asset_is_cached_for_good(ui_client: TestClient) -> None:
    """A file under `/assets/` has a content hash in its name, so it never changes."""
    response = ui_client.get("/assets/app-1a2b.js")
    assert response.status_code == 200
    assert response.text == "console.log('app');"
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    _assert_ui_headers(response)
    assert ui_client.head("/assets/app-1a2b.js").status_code == 200


def test_a_font_has_its_own_content_type(ui_client: TestClient) -> None:
    """Every served type is named, so `nosniff` never blocks one."""
    response = ui_client.get("/assets/plex-9z8y.woff2")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"


@pytest.mark.parametrize(
    "path",
    ["/theme-boot.js", "/licenses/IBM-Plex-OFL-1.1.txt", "/licenses/THIRD-PARTY-NOTICES.md"],
)
def test_a_file_outside_assets_is_revalidated(ui_client: TestClient, path: str) -> None:
    """Root files and licences keep their names across builds, so a browser asks again."""
    response = ui_client.get(path)
    assert response.status_code == 200
    assert MARKER not in response.text
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "text/plain"}
    _assert_ui_headers(response)


def test_an_unchanged_asset_is_not_sent_again(ui_client: TestClient) -> None:
    """A revalidation answers 304, still with the interface's headers and the cache rule."""
    etag = ui_client.get("/assets/app-1a2b.js").headers["etag"]
    response = ui_client.get("/assets/app-1a2b.js", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    _assert_ui_headers(response)


def test_part_of_an_asset_keeps_its_type(ui_client: TestClient) -> None:
    """A range request gets the part with the asset's own content type."""
    response = ui_client.get("/assets/app-1a2b.js", headers={"Range": "bytes=0-6"})
    assert response.status_code == 206
    assert response.text == "console"
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"


def test_an_unsatisfiable_range_is_not_cached(ui_client: TestClient) -> None:
    """An error answer of an asset is never cached for good."""
    response = ui_client.get("/assets/app-1a2b.js", headers={"Range": "bytes=900-999"})
    assert response.status_code == 416
    assert "immutable" not in response.headers.get("cache-control", "")


def test_another_method_on_an_asset_is_refused(ui_client: TestClient) -> None:
    """Files are read-only: a POST is 405 with the interface's headers."""
    response = ui_client.post("/assets/app-1a2b.js")
    assert response.status_code == 405
    assert response.text == "Method Not Allowed"
    _assert_ui_headers(response)


def test_only_plain_root_file_names_are_served(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hidden file, a name with route syntax, or one named like an API segment is no file."""
    (ui_dist / ".env").write_text(SECRET)
    (ui_dist / "{x}.js").write_text(SECRET)
    (ui_dist / "healthz").write_text(SECRET)
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        for path in ("/.env", "/%7Bx%7D.js", "/healthz"):
            response = c.get(path)
            assert SECRET not in response.text, path
        assert c.get("/healthz").json() == {"status": "ok"}


def test_a_build_without_licences_serves_none(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The licences directory is optional; without it every licence path is 404."""
    for licence in (ui_dist / "licenses").iterdir():
        licence.unlink()
    (ui_dist / "licenses").rmdir()
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        assert c.get("/licenses/IBM-Plex-OFL-1.1.txt").status_code == 404


def test_behind_a_path_prefix_the_api_keeps_its_answers(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a proxy's path prefix (`root_path`), an API path is still never the page."""
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", ui_dist)
    app = create_app(settings.model_copy(update={"ui": UiConfig(enabled=True)}))
    with TestClient(app, root_path="/lakana") as c:
        response = c.get("/lakana/api/v1/nope")
        assert response.status_code == 404
        assert MARKER not in response.text
        assert MARKER in c.get("/lakana/catalog").text


@pytest.mark.parametrize(
    "path", ["/assets/missing.js", "/assets/missing.png", "/assets/", "/licenses/other.txt"]
)
def test_a_missing_file_is_404_not_the_index(ui_client: TestClient, path: str) -> None:
    """A missing file is 404, never the page (a browser would run HTML as a script)."""
    response = ui_client.get(path)
    assert response.status_code == 404
    assert MARKER not in response.text
    assert "immutable" not in response.headers.get("cache-control", "")
    _assert_ui_headers(response)


@pytest.mark.parametrize(
    "path",
    [
        "/assets/%2e%2e/index.html",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/assets/..%2f..%2fsettings.py",
        "/assets/../../ui.py",
        "/assets/..%5c..%5csecret.txt",
        "/assets/%5c..%5c..%5csecret.txt",
        "/assets//etc/passwd",
        "/assets/%2fetc%2fpasswd",
        f"/assets/{'/' * 3}{Path.cwd()}/pyproject.toml",
        "/assets/app-1a2b.js%00.png",
        "/licenses/..%2f..%2fsecret.txt",
        "/licenses/%2e%2e/%2e%2e/secret.txt",
        "/licenses/%2fetc%2fpasswd",
        "/licenses/IBM-Plex-OFL-1.1.txt%00",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/theme-boot.js%00",
        "/%5c..%5csecret.txt",
    ],
)
def test_a_path_outside_the_build_is_refused(ui_client: TestClient, path: str) -> None:
    """No spelling of a path outside the build reads a file there.

    The answer is 404, or the page itself (no file) where the path is an app path.
    """
    response = ui_client.get(path)
    assert SECRET not in response.text
    assert "root:" not in response.text
    assert "[project]" not in response.text
    assert response.status_code in {200, 404}, response.status_code
    if response.status_code == 200:
        assert response.text == INDEX
        assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("where", ["assets/out.js", "licenses/out.txt", "out.js"])
def test_a_symlink_out_of_the_build_is_not_followed(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A link inside the build that points outside it is never served."""
    (ui_dist / where).symlink_to(ui_dist.parent / "secret.txt")
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        response = c.get(f"/{where}")
    assert SECRET not in response.text
    assert response.status_code in {200, 404}
    if response.status_code == 200:
        assert response.text == INDEX


def test_a_symlinked_directory_out_of_the_build_is_not_followed(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked directory under `licenses/` or `assets/` does not open the outside."""
    (ui_dist / "licenses" / "up").symlink_to(ui_dist.parent, target_is_directory=True)
    (ui_dist / "assets" / "up").symlink_to(ui_dist.parent, target_is_directory=True)
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        for path in ("/licenses/up/secret.txt", "/assets/up/secret.txt"):
            response = c.get(path)
            assert response.status_code == 404, path
            assert SECRET not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/nope"),
        ("GET", "/api/nope"),
        ("GET", "/api"),
        ("POST", "/api/v1/nope"),
        ("DELETE", "/api/v1/whoami"),
        ("HEAD", "/api/v1/nope"),
        ("GET", "/v1/nope"),
        ("GET", "/v1/config"),
        ("GET", "/v1/sales.orders"),
        ("GET", "/healthz"),
        ("POST", "/healthz"),
        ("GET", "/healthz/extra"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/docs/oauth2-redirect"),
    ],
)
def test_turning_the_interface_on_changes_no_api_answer(
    client: TestClient, ui_client: TestClient, method: str, path: str
) -> None:
    """An API path answers exactly as without the interface: same status, body and type."""
    off = client.request(method, path)
    on = ui_client.request(method, path)
    assert (on.status_code, on.headers.get("content-type"), on.content) == (
        off.status_code,
        off.headers.get("content-type"),
        off.content,
    )
    assert MARKER not in on.text


def test_api_responses_carry_no_ui_headers(ui_client: TestClient) -> None:
    """The interface's headers are for its pages; an API answer keeps its own."""
    for response in (
        ui_client.get("/api/v1/whoami"),
        ui_client.get("/api/v1/whoami", headers=AUTH_HEADER),
        ui_client.get("/api/v1/nope"),
    ):
        assert "content-security-policy" not in response.headers
        assert "x-frame-options" not in response.headers


def test_the_openapi_paths_are_the_same_with_the_interface(
    client: TestClient, ui_client: TestClient
) -> None:
    """The interface's routes are not part of the API's description.

    FastAPI describes every `APIRoute` with `include_in_schema`; the interface adds routes
    after the API's and none of them is one (generating both documents would take a second).
    """
    plain = cast(FastAPI, client.app).router.routes
    with_ui = cast(FastAPI, ui_client.app).router.routes
    assert [type(r) for r in with_ui[: len(plain)]] == [type(r) for r in plain]
    added = with_ui[len(plain) :]
    assert len(added) == 4  # assets, licences, theme-boot.js, the pages
    for route in added:
        assert not isinstance(route, APIRoute)
        assert getattr(route, "include_in_schema", False) is False


def test_reserved_segments_cover_the_api(client: TestClient) -> None:
    """Every first segment the API uses is reserved, so the page never answers there."""
    reserved = reserved_segments(cast(FastAPI, client.app))
    assert reserved >= {"api", "v1", "healthz", "readyz", "docs", "redoc", "openapi.json"}


def test_enabled_without_a_build_refuses_to_start(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turned on without built files, the server stops and says how to get them."""
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", tmp_path / "no-build")
    with pytest.raises(UiNotBuiltError) as raised:
        create_app(settings.model_copy(update={"ui": UiConfig(enabled=True)}))
    message = str(raised.value)
    assert "LAKANA_UI__ENABLED" in message
    assert "npm --prefix ui run build" in message
    assert str(tmp_path / "no-build") in message
    assert "LAKANA_UI__ENABLED=false" in message


def test_the_sdist_and_the_wheel_carry_the_built_interface() -> None:
    """Hatch leaves the git-ignored build out unless `artifacts` names it, for both targets."""
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    build = pyproject["tool"]["hatch"]["build"]
    assert "src/lakana_server/ui_dist/**" in build["artifacts"]
    for target in ("wheel", "sdist"):
        assert "artifacts" not in build.get("targets", {}).get(target, {}), target


def test_the_header_is_the_policy_the_build_wrote(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source: whatever `csp.txt` the build carries is the header, trailing newline aside."""
    other = "default-src 'none'"
    (ui_dist / CSP_FILE).write_text(other + "\n")
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        for path in ("/", "/catalog", "/assets/app-1a2b.js", "/theme-boot.js", "/assets/nope"):
            assert c.get(path).headers["content-security-policy"] == other, path


def test_the_policy_file_is_not_a_page_of_its_own(ui_client: TestClient) -> None:
    """`csp.txt` is read at start-up, not served: its path is an app path like any other."""
    response = ui_client.get(f"/{CSP_FILE}")
    assert response.status_code == 200
    assert response.text == INDEX


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (None, "is missing"),
        ("", "is empty"),
        ("   \n", "is empty"),
        ("default-src 'self'\nX-Injected: 1", "control character"),
        ("default-src 'self'\r", "control character"),
        ("default-src 'self'\x00", "control character"),
        ("default-src 'sélf'", "not plain ASCII"),
        ("script-src 'self'", "has no default-src"),
    ],
)
def test_a_missing_or_malformed_policy_refuses_to_start(
    ui_dist: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    cause: str,
) -> None:
    """Without a policy it can send as one header, the server stops and says how to fix it."""
    if content is None:
        (ui_dist / CSP_FILE).unlink()
    else:
        (ui_dist / CSP_FILE).write_bytes(content.encode())
    with pytest.raises(UiNotBuiltError) as raised:
        _ui_client(settings, monkeypatch, ui_dist)
    message = str(raised.value)
    assert cause in message
    assert str(ui_dist / CSP_FILE) in message
    assert "npm --prefix ui run build" in message
    assert "LAKANA_UI__ENABLED=false" in message


def test_a_policy_that_is_not_text_refuses_to_start(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes that are not UTF-8 are a broken build, named as such."""
    (ui_dist / CSP_FILE).write_bytes(b"default-src \xff")
    with pytest.raises(UiNotBuiltError, match="not plain ASCII"):
        _ui_client(settings, monkeypatch, ui_dist)


def test_a_policy_linked_from_outside_the_build_refuses_to_start(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`csp.txt` must be the build's own file, never one a link points to elsewhere."""
    outside = ui_dist.parent / "other-policy.txt"
    outside.write_text("default-src *")
    (ui_dist / CSP_FILE).unlink()
    (ui_dist / CSP_FILE).symlink_to(outside)
    with pytest.raises(UiNotBuiltError, match="is a link"):
        _ui_client(settings, monkeypatch, ui_dist)


@pytest.mark.parametrize("policy", ["DEFAULT-SRC 'self'", "Default-Src 'none'; img-src 'self'"])
def test_directive_names_are_read_in_any_case(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    """Browsers read directive names case-insensitively, and so does the start-up check."""
    (ui_dist / CSP_FILE).write_text(policy)
    with _ui_client(settings, monkeypatch, ui_dist) as c:
        assert c.get("/").headers["content-security-policy"] == policy


def test_an_empty_segment_inside_an_app_path_is_no_page(ui_client: TestClient) -> None:
    """Only a trailing slash may leave an empty segment; `/catalog/` is a page, `//x` is not."""
    assert MARKER.encode() in _raw_get(ui_client.app, "/catalog/")[2]
    assert _raw_get(ui_client.app, "//catalog")[0] == 404


def test_an_unreadable_policy_refuses_to_start(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy path the server cannot read names the system's reason."""
    (ui_dist / CSP_FILE).unlink()
    (ui_dist / CSP_FILE).mkdir()
    with pytest.raises(UiNotBuiltError, match="cannot be read"):
        _ui_client(settings, monkeypatch, ui_dist)


def test_a_build_without_assets_refuses_to_start(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial build would answer every script with an error; it stops the start instead."""
    for path in (ui_dist / "assets").iterdir():
        path.unlink()
    (ui_dist / "assets").rmdir()
    with pytest.raises(UiNotBuiltError, match=r"has no assets/ directory"):
        _ui_client(settings, monkeypatch, ui_dist)


def test_the_pages_are_the_last_route_and_every_api_segment_is_reserved(
    ui_client: TestClient, client: TestClient
) -> None:
    """Nothing registered after the interface, and every API route is outside its pages.

    A route added after `mount_ui` would be shadowed by the pages; a path-parameter first
    segment could not be reserved at all.
    """
    routes = cast(FastAPI, ui_client.app).router.routes
    pages = routes[-1]
    assert isinstance(pages, lakana_server.ui._AppPages)
    api = cast(FastAPI, client.app)
    firsts = {
        (route.path or "").lstrip("/").split("/", 1)[0]
        for route in iter_route_contexts(api.router.routes)
    }
    assert firsts - {""} <= pages.reserved
    assert not any(segment.startswith("{") for segment in pages.reserved)


def test_an_api_route_with_a_parameter_first_cannot_be_served_beside() -> None:
    """A route such as `/{tenant}/...` would swallow app paths; mounting refuses it."""
    app = FastAPI()

    @app.get("/{tenant}/things")
    def things(tenant: str) -> str:
        """Stand for an API route whose first segment is a parameter.

        Parameters
        ----------
        tenant
            The parameter.

        Returns
        -------
        str
            The parameter.
        """
        return tenant

    with pytest.raises(RuntimeError, match=r"/\{tenant\}/things"):
        reserved_segments(app)


def _raw_get(app: object, path: str, root_path: str = "") -> tuple[int, dict[str, str], bytes]:
    """Send one GET with a path exactly as given, as a server delivers it after unquoting.

    A client such as httpx removes `.` and `..` segments before sending; this does not.

    Parameters
    ----------
    app
        The started ASGI application.
    path
        The decoded request path.
    root_path
        The ASGI `root_path`.

    Returns
    -------
    tuple of int, dict of str to str, and bytes
        The status, the headers (lower-case names) and the body.
    """
    sent: list[Message] = []

    async def receive() -> Message:  # ruff: ignore[unused-async]
        """Hand over an empty request body.

        Returns
        -------
        dict
            The one `http.request` message.
        """
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:  # ruff: ignore[unused-async]
        """Keep a message the application sends.

        Parameters
        ----------
        message
            The ASGI message.
        """
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8", "surrogateescape"),
        "root_path": root_path,
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }

    async def call() -> None:
        """Run the application once on this request."""
        await cast(ASGIApp, app)(scope, receive, send)

    asyncio.run(call())
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], headers, body


@pytest.mark.parametrize(
    "path",
    [
        "/assets/../index.html",
        "/assets/../../secret.txt",
        "/assets/../../ui.py",
        "/assets/..\\..\\secret.txt",
        "/assets//etc/passwd",
        "/assets/app-1a2b.js\x00",
        "/licenses/../../secret.txt",
        "/licenses/../index.html",
        "/../secret.txt",
        "/../../etc/passwd",
        "/.\\..\\secret.txt",
    ],
)
def test_a_raw_path_outside_the_build_is_refused(ui_client: TestClient, path: str) -> None:
    """Literal dot segments, which a client would remove, reach no file outside the build."""
    status, headers, body = _raw_get(ui_client.app, path)
    assert SECRET.encode() not in body
    assert b"root:" not in body
    assert status == 404, (status, body[:60])
    assert MARKER.encode() not in body
    assert headers.get("content-security-policy") in {POLICY, None}


@pytest.mark.parametrize(
    "path",
    [
        "/./api/v1/nope",
        "/API/v1/nope",
        "/Api/V1/whoami",
        "/api\\v1/nope",
        "/../api/v1/nope",
        "/V1/nope",
        "/HEALTHZ",
        "/catalog/../api/v1/nope",
        "//api/v1/nope",
        "///api/v1/nope",
        "//v1/config",
        "//healthz",
        "//API/v1",
        "/\\/api",
        "//./api",
        "/catalog//api/v1/nope",
    ],
)
def test_an_unnormalised_api_path_answers_as_without_the_interface(
    client: TestClient, ui_client: TestClient, path: str
) -> None:
    """A path a client would not send still gets the API's answer, never the page."""
    off = _raw_get(client.app, path)
    on = _raw_get(ui_client.app, path)
    assert (on[0], on[1].get("content-type"), on[2]) == (
        off[0],
        off[1].get("content-type"),
        off[2],
    )
    assert MARKER.encode() not in on[2]


def test_a_prefix_is_stripped_only_at_a_segment_boundary(ui_client: TestClient) -> None:
    """Behind `root_path="/lakana"`, `/lakana/api/...` stays the API's; app paths are pages."""
    status, _, body = _raw_get(ui_client.app, "/lakana/api/v1/nope", root_path="/lakana")
    assert status == 404
    assert MARKER.encode() not in body
    status, _, body = _raw_get(ui_client.app, "/lakana/catalog", root_path="/lakana")
    assert status == 200
    assert MARKER.encode() in body


@pytest.mark.parametrize(
    ("method", "status", "text"), [("POST", 405, "Method Not Allowed"), ("GET", 404, "Not Found")]
)
def test_an_asset_error_names_its_own_status(
    ui_client: TestClient, method: str, status: int, text: str
) -> None:
    """The text is the status's own phrase, and a 405 says which methods work."""
    response = ui_client.request(method, "/assets/app-1a2b.js" if status == 405 else "/assets/x")
    assert response.status_code == status
    assert response.text == text
    if status == 405:
        assert response.headers["allow"] == "GET, HEAD"
    else:
        assert "allow" not in response.headers
