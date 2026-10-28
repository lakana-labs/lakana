"""Items from a source REST API, read with your own HTTPS client."""

import os
import ssl

import httpx
import pyarrow as pa

from lakana import asset
from lakana.ingest import connections


@asset(connections=["source-api"])
def items():
    """Every item, read with httpx from what the connection holds."""
    api = connections.get("source-api")
    # Trust what the helpers trust: the workers' bundle (the system's roots without one) and the
    # connection's own CA.
    trust = ssl.create_default_context(cafile=os.environ.get("LAKANA_CA_BUNDLE"))
    trust.minimum_version = ssl.TLSVersion.TLSv1_2
    if api.settings.get("ca_certificate"):
        trust.load_verify_locations(cadata=api.settings["ca_certificate"])
    base = httpx.URL(str(api.address["base_url"]).rstrip("/") + "/")
    rows, url = [], base.join("items")
    with httpx.Client(
        headers={"Authorization": f"Bearer {api.secret['token']}"},
        proxy=api.proxy_url,  # the task's egress proxy under the Docker runner; None otherwise
        trust_env=False,  # no proxy, certificate or .netrc from the task's environment
        verify=trust,
        timeout=30,
    ) as client:
        while url is not None:
            # The token goes with every request: never send it below another address.
            if not str(url).startswith(str(base)):
                raise ValueError(f"the next link {url} is not below {base}")
            page = client.get(url)
            page.raise_for_status()
            body = page.json()
            rows += body["value"]
            link = body.get("@odata.nextLink")
            url = None if link is None else url.join(link)
    return pa.Table.from_pylist(rows)
