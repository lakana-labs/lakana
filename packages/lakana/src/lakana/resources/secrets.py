"""The `secrets` resource: `Client.secrets`.

Secrets are always addressed through their environment, matching
`lakana_server.api.platform.secrets`'s own nesting — a secret has no meaning
outside the environment it is scoped to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import Secret, SecretCreate
from lakana._routes import ROUTES


def _secret_from_json(data: dict[str, Any]) -> Secret:
    """Build a `Secret` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `SecretOut` JSON body. Structurally cannot carry a
        value or ciphertext field. A server that predates the stored-format
        fields sends neither `format_version` nor `key_id`; they read as `1`
        and `env`, which is how such a server stores every secret, and neither `use` nor
        `use_target`, `use_target_url` or `rotated_at`, which read as `None`.

    Returns
    -------
    Secret
        The parsed secret metadata.
    """
    rotated_at = data.get("rotated_at")
    return Secret(
        id=data["id"],
        name=data["name"],
        environment=data["environment"],
        created_by=data["created_by"],
        created_at=datetime.fromisoformat(data["created_at"]),
        format_version=data.get("format_version", 1),
        key_id=data.get("key_id", "env"),
        use=data.get("use"),
        use_target=data.get("use_target"),
        use_target_url=data.get("use_target_url"),
        rotated_at=None if rotated_at is None else datetime.fromisoformat(rotated_at),
    )


_SecretList = list[Secret]
"""Alias for `list[Secret]`, resolved at module scope.

`Secrets` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias is resolved before that happens, and referenced by name instead.
"""


class Secrets:
    """The `secrets` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        environment: str,
        name: str,
        values: dict[str, str],
        kind: str | None = None,
        target: str | None = None,
    ) -> Secret:
        """Create a secret.

        Parameters
        ----------
        environment
            The environment this secret is scoped to.
        name
            The secret's name, unique within its environment.
        values
            The plaintext key-value bag to encrypt and store. Sent only in
            the request body — never echoed back in any response, and never
            held by the SDK afterward.
        kind
            What the secret is for, so that it is bound as it is created: ``package_index``,
            ``repository`` or ``environment``. `None` leaves it unbound.
        target
            The index's or repository's name; `None` for the environment.

        Returns
        -------
        Secret
            The created secret's metadata.

        Raises
        ------
        lakana.errors.NotFound
            If the environment, or the named index or repository, does not exist.
        lakana.errors.Conflict
            If a secret of this name already exists in this environment.
        """
        request = SecretCreate(name=name, values=values)
        response = self._transport.request(
            ROUTES["secrets.create"],
            path_params={"environment_name": environment},
            json={
                "name": request.name,
                "values": request.values,
                **({"use": {"kind": kind, "target": target}} if kind is not None else {}),
            },
        )
        return _secret_from_json(response.json())

    def list(self, environment: str) -> _SecretList:
        """List an environment's active secrets.

        Parameters
        ----------
        environment
            The environment whose secrets to list.

        Returns
        -------
        list of Secret
            Every active secret in this environment.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        """
        response = self._transport.request(
            ROUTES["secrets.list"], path_params={"environment_name": environment}
        )
        return [_secret_from_json(item) for item in response.json()]

    def rotate(self, environment: str, name: str, values: dict[str, str]) -> Secret:
        """Rotate a secret's value.

        Parameters
        ----------
        environment
            The environment the secret is scoped to.
        name
            The secret's name.
        values
            The new plaintext key-value bag.

        Returns
        -------
        Secret
            The rotated secret's metadata.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the secret name doesn't exist.
        """
        response = self._transport.request(
            ROUTES["secrets.rotate"],
            path_params={"environment_name": environment, "secret_name": name},
            json={"values": values},
        )
        return _secret_from_json(response.json())

    def revoke(self, environment: str, name: str) -> None:
        """Revoke a secret.

        Parameters
        ----------
        environment
            The environment the secret is scoped to.
        name
            The secret's name.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the secret name doesn't exist.
        """
        self._transport.request(
            ROUTES["secrets.revoke"],
            path_params={"environment_name": environment, "secret_name": name},
        )

    def bind(self, environment: str, name: str, kind: str, target: str | None = None) -> Secret:
        """Bind a secret to the one use it may serve.

        Parameters
        ----------
        environment
            The environment the secret is scoped to.
        name
            The secret's name.
        kind
            ``package_index``, ``repository``, ``connection`` or ``environment``. A connection's
            secret becomes the one the connection uses.
        target
            The index's, repository's or connection's name; `None` for the environment.

        Returns
        -------
        Secret
            The bound secret's metadata.

        Raises
        ------
        lakana.errors.NotFound
            If the environment, the secret or the target does not exist.
        lakana.errors.PermissionDenied
            If the caller may not manage secrets in the environment.
        """
        response = self._transport.request(
            ROUTES["secrets.bind"],
            path_params={"environment_name": environment, "secret_name": name},
            json={"kind": kind, "target": target},
        )
        return _secret_from_json(response.json())
