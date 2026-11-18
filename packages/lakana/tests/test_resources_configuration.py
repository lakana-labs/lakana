"""The SDK's reading of the configuration answer."""

from __future__ import annotations

import httpx

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.resources.configuration import Configuration

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")


def test_list_reads_name_value_secret_has_value_and_source() -> None:
    body = [
        {"name": "LAKANA_PORT", "value": 8000, "secret": False, "has_value": True, "source": "set"},
        {
            "name": "LAKANA_DATABASE__URL",
            "value": None,
            "secret": True,
            "has_value": True,
            "source": "default",
        },
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    settings = Configuration(HttpTransport(_CONFIG, http_client=client)).list()
    assert (seen[0].method, seen[0].url.path) == ("GET", "/api/v1/settings/configuration")
    assert [(s.name, s.value, s.secret, s.has_value, s.source) for s in settings] == [
        ("LAKANA_PORT", 8000, False, True, "set"),
        ("LAKANA_DATABASE__URL", None, True, True, "default"),
    ]
