"""A stand-in browser for Keycloak in a container, for the OIDC sign-in integration test.

Keycloak runs on the container's bridge address under the name ``keycloak.test``, which no
resolver knows. The browser sends each request to that address with the ``Host`` header and
the TLS server name ``keycloak.test``, exactly as `lakana_core.net.egress.pinned_request` builds
them for the server's own requests, so the certificate is verified against the name. It keeps
Keycloak's cookies (its sign-in session), submits Keycloak's login form, follows only the
redirects that stay on ``keycloak.test``, and stops at the first one that leaves it: the
redirect back to Lakana's callback.
"""

from __future__ import annotations

import html
import re
import ssl
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from lakana_core.net.egress import pinned_request

_FORM = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', re.DOTALL)
"""The action of Keycloak's login form."""

_MAX_REDIRECTS = 10


class KeycloakBrowser:
    """A browser that talks to one Keycloak by its name, at a fixed address.

    Parameters
    ----------
    address
        The container's address.
    host
        The name Keycloak is known by, such as ``keycloak.test``.
    ca_file
        The PEM file of the authority that signed Keycloak's certificate.
    timeout
        Seconds each request may take.
    """

    def __init__(self, address: str, host: str, ca_file: Path, timeout: float = 30.0) -> None:
        self.address = address
        self.host = host
        self.ca_file = ca_file
        self._client = httpx.Client(
            verify=ssl.create_default_context(cafile=str(ca_file)),
            follow_redirects=False,
            timeout=timeout,
        )

    def close(self) -> None:
        """Close the connections."""
        self._client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        json: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request to Keycloak.

        Parameters
        ----------
        method
            The HTTP method.
        url
            An ``https`` URL on `host`.
        data
            A form to send.
        json
            A JSON body to send.
        headers
            Further headers.

        Returns
        -------
        httpx.Response
            The answer, redirects not followed.
        """
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == self.host, url
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        pinned, host_header, extensions = pinned_request(
            "https", self.host, parts.port or 443, target, self.address
        )
        return self._client.request(
            method,
            pinned,
            data=data,
            json=json,
            headers={**(headers or {}), "Host": host_header},
            extensions=extensions,
        )

    def follow(self, response: httpx.Response) -> httpx.Response | str:
        """Follow Keycloak's redirects that stay on Keycloak.

        Parameters
        ----------
        response
            An answer of Keycloak.

        Returns
        -------
        httpx.Response or str
            The first answer that is no redirect, or the first redirect's address that leaves
            Keycloak (the way back to Lakana).

        Raises
        ------
        AssertionError
            After more redirects than a sign-in takes.
        """
        for _ in range(_MAX_REDIRECTS):
            if not response.is_redirect:
                return response
            location = response.headers["location"]
            if urlsplit(location).hostname != self.host:
                return location
            response = self.request("GET", location)
        raise AssertionError("Keycloak redirected too often")

    def get(self, url: str) -> httpx.Response | str:
        """Open a Keycloak address, following its redirects.

        Parameters
        ----------
        url
            The address, such as Lakana's authorization URL.

        Returns
        -------
        httpx.Response or str
            See `follow`.
        """
        return self.follow(self.request("GET", url))

    def submit_login(
        self, page: httpx.Response, username: str, password: str
    ) -> httpx.Response | str:
        """Fill in and send Keycloak's login form.

        Parameters
        ----------
        page
            The login page.
        username
            The user name.
        password
            The password.

        Returns
        -------
        httpx.Response or str
            See `follow`: Lakana's callback address after a sign-in, a page otherwise.
        """
        found = _FORM.search(page.text)
        assert found is not None, page.text[:2000]
        action = html.unescape(found.group(1))
        sent = self.request(
            "POST", action, data={"username": username, "password": password, "credentialId": ""}
        )
        return self.follow(sent)

    def sign_in(self, url: str, username: str, password: str) -> httpx.Response | str:
        """Open an authorization URL and sign in at Keycloak's login form.

        Parameters
        ----------
        url
            The authorization URL.
        username
            The user name.
        password
            The password.

        Returns
        -------
        httpx.Response or str
            See `submit_login`.
        """
        page = self.get(url)
        assert isinstance(page, httpx.Response), f"Keycloak did not ask to sign in: {page}"
        return self.submit_login(page, username, password)
