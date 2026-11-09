"""The real build of the interface, served by the real app.

`test_ui_serving.py` serves a fixture build on purpose; this module serves whatever
`npm --prefix ui run build` wrote to `lakana_server.ui.UI_DIST`, so the seam between the build
and the server is checked too. Without a build it is skipped; CI's `ui` job runs it right after
the build.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from lakana_server.ui import CSP_FILE, UI_DIST, content_type

pytestmark = pytest.mark.skipif(
    not (UI_DIST / "index.html").is_file(),
    reason="no build of the interface: run `npm --prefix ui run build` first",
)

_LINK = re.compile(r"""\b(?:src|href)="(/[^"]*)\"""")
_CSS_URL = re.compile(r"""url\(\s*['"]?(/[^'")]+)['"]?\s*\)""")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client for the app serving the real build.

    Parameters
    ----------
    settings
        The test settings, turned on for the interface here.

    Yields
    ------
    TestClient
        A started client.
    """
    app = create_app(settings.model_copy(update={"ui": UiConfig(enabled=True)}))
    # The public address of the test settings: the Host guard refuses any other name.
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def test_the_page_carries_the_built_policy(client: TestClient) -> None:
    """`/` is the built page, sent with the policy the build wrote, byte for byte."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    policy = (UI_DIST / CSP_FILE).read_text().strip()
    assert response.headers["content-security-policy"] == policy


def test_everything_the_page_loads_is_served(client: TestClient) -> None:
    """Every `src`/`href` of the page and every `url()` of its CSS answers 200 with its type."""
    links = _LINK.findall(client.get("/").text)
    assert links, "the built page links nothing"
    stylesheets = [link for link in links if link.endswith(".css")]
    assert stylesheets, "the built page links no stylesheet"
    for sheet in stylesheets:
        links += _CSS_URL.findall(client.get(sheet).text)
    for link in links:
        response = client.get(link)
        assert response.status_code == 200, link
        expected = content_type(UI_DIST / link.lstrip("/").split("?")[0])
        assert response.headers["content-type"] == expected, link


def test_the_licence_notices_are_served(client: TestClient) -> None:
    """The bundled packages' notices and the fonts' licence are readable in a browser."""
    for path in ("/licenses/THIRD-PARTY-NOTICES.md", "/licenses/IBM-Plex-OFL-1.1.txt"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"] == "text/plain; charset=utf-8", path
