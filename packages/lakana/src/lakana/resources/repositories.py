"""The `repositories` resource: `Client.repositories`."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import DeployKey, DeployKeys, HostKey, Repository, RepositoryProbe
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse
from lakana.resources.artifacts import _ODD_ANSWER, _object

_monotonic = time.monotonic
"""The clock a wait counts on (a seam for the tests)."""

_sleep = time.sleep
"""The pause between two polls (a seam for the tests)."""


def _probe_from_json(data: object) -> RepositoryProbe:
    """Build a `RepositoryProbe` from a `RepositoryProbeOut` body.

    A field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    RepositoryProbe
        The check.

    Raises
    ------
    InvalidResponse
        If `data` is not an object with every field of a check.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        finished, keys = data["finished_at"], data["host_keys"]
        return RepositoryProbe(
            id=str(data["id"]),
            kind=str(data["kind"]),
            state=str(data["state"]),
            outcome=data["outcome"],
            message=data["message"],
            host_keys=None
            if keys is None
            else [
                HostKey(type=str(k["type"]), fingerprint=str(k["fingerprint"]), line=str(k["line"]))
                for k in keys
            ],
            created_at=datetime.fromisoformat(data["created_at"]),
            finished_at=None if finished is None else datetime.fromisoformat(finished),
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a repository check")) from None


UPDATABLE = frozenset(
    {"url", "credential_secret", "credential_username", "ssh_known_hosts", "default_ref"}
)
"""Fields `update` may change; mirrors the server."""


def _repository_from_json(data: dict[str, Any]) -> Repository:
    """Build a `Repository` from a `RepositoryOut` body.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    Repository
        The repository.
    """
    return Repository(
        id=data["id"],
        name=data["name"],
        kind=data["kind"],
        url=data["url"],
        credential_secret=data["credential_secret"],
        credential_username=data["credential_username"],
        ssh_known_hosts=data["ssh_known_hosts"],
        default_ref=data["default_ref"],
        created_by=data["created_by"],
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        deploy_keys=[_deploy_key_from_json(k) for k in data.get("deploy_keys") or ()],
    )


def _deploy_key_from_json(data: dict[str, Any]) -> DeployKey:
    """Build a `DeployKey` from one key of an answer.

    Parameters
    ----------
    data
        A `RepositoryDeployKeyOut` or `DeployKeyMadeOut` body.

    Returns
    -------
    DeployKey
        The key; `made` is `False` when the body has none.
    """
    return DeployKey(
        environment=str(data["environment"]),
        public_key=str(data["public_key"]),
        fingerprint=str(data["fingerprint"]),
        made=bool(data.get("made")),
    )


_RepositoryList = list[Repository]
"""`list[Repository]` resolved before `Repositories.list` shadows the builtin."""


class Repositories:
    """The `repositories` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        name: str,
        kind: str,
        url: str,
        *,
        credential_secret: str | None = None,
        credential_username: str | None = None,
        ssh_known_hosts: str | None = None,
        default_ref: str | None = None,
    ) -> Repository:
        """Register a repository (needs `MANAGE_REPOSITORY`).

        Parameters
        ----------
        name
            Unique name; no dots.
        kind
            ``local``, ``https`` or ``ssh``.
        url
            URL, or an absolute path for ``local``.
        credential_secret
            Secret name resolved per environment.
        credential_username
            HTTPS username; default ``oauth2``.
        ssh_known_hosts
            Required for ``ssh``.
        default_ref
            Ref deployed when none is given.

        Returns
        -------
        Repository
            The registered repository.

        Raises
        ------
        lakana.errors.Conflict
            If the name is taken.
        lakana.errors.ValidationError
            If a field is invalid.
        """
        response = self._transport.request(
            ROUTES["repositories.create"],
            json={
                "name": name,
                "kind": kind,
                "url": url,
                "credential_secret": credential_secret,
                "credential_username": credential_username,
                "ssh_known_hosts": ssh_known_hosts,
                "default_ref": default_ref,
            },
        )
        return _repository_from_json(response.json())

    def list(self) -> _RepositoryList:
        """List repositories.

        Returns
        -------
        list of Repository
            Sorted by name.
        """
        response = self._transport.request(ROUTES["repositories.list"])
        return [_repository_from_json(item) for item in response.json()]

    def get(self, name: str) -> Repository:
        """Show a repository.

        Parameters
        ----------
        name
            Its name.

        Returns
        -------
        Repository
            The repository.

        Raises
        ------
        lakana.errors.NotFound
            If none has that name.
        """
        response = self._transport.request(ROUTES["repositories.get"], path_params={"name": name})
        return _repository_from_json(response.json())

    def update(self, name: str, **changes: str | None) -> Repository:
        """Change a repository; `None` clears a field.

        Parameters
        ----------
        name
            Its name.
        **changes
            Any of `url`, `credential_secret`, `credential_username`,
            `ssh_known_hosts`, `default_ref`.

        Returns
        -------
        Repository
            The updated repository.

        Raises
        ------
        TypeError
            For any other keyword.
        """
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise TypeError(f"update() got unexpected field(s): {', '.join(sorted(unknown))}")
        response = self._transport.request(
            ROUTES["repositories.update"], path_params={"name": name}, json=changes
        )
        return _repository_from_json(response.json())

    def delete(self, name: str) -> None:
        """Delete a repository.

        Parameters
        ----------
        name
            Its name.

        Raises
        ------
        lakana.errors.Conflict
            While any environment deploys it.
        """
        self._transport.request(ROUTES["repositories.delete"], path_params={"name": name})

    def deploy_key(
        self,
        name: str,
        *,
        secret: str | None = None,
        environments: Sequence[str] | None = None,
        replace: bool = False,
    ) -> DeployKeys:
        """Make an SSH deploy key for each environment; the server keeps the private halves.

        Needs `MANAGE_REPOSITORY` and `MANAGE_SECRET` in each environment written, and an ssh
        repository with confirmed host keys. By default every environment you may manage
        secrets in that has no key yet gets one; add each public key at your git host as a
        read-only deploy key.

        Parameters
        ----------
        name
            The repository's name.
        secret
            The secret name; ``<name>-deploy-key`` by default.
        environments
            Only these environments.
        replace
            Replace the keys of `environments` (required with it); the old keys stop working.

        Returns
        -------
        DeployKeys
            The public keys, by environment.

        Raises
        ------
        lakana.errors.ValidationError
            For a repository that is not ssh or has no host keys, or `replace` alone.
        lakana.errors.Conflict
            If a secret of the name is revoked or is not a key made for this repository.
        lakana.errors.PermissionDenied
            Without the rights above.
        InvalidResponse
            If the answer is not a deploy key answer.
        """
        body: dict[str, object] = {}
        if secret is not None:
            body["secret"] = secret
        if environments:
            body["environments"] = list(environments)
        if replace:
            body["replace"] = True
        response = self._transport.request(
            ROUTES["repositories.deploy_key"], path_params={"name": name}, json=body
        )
        data = _object(response, "a deploy key")
        try:
            return DeployKeys(
                secret=str(data["secret"]),
                keys=[_deploy_key_from_json(k) for k in data["keys"]],
                skipped=[str(e) for e in data["skipped"]],
                replaced=[str(e) for e in data["replaced"]],
                notes=[str(n) for n in data["notes"]],
            )
        except (KeyError, TypeError, ValueError):
            raise InvalidResponse(_ODD_ANSWER.format(what="a deploy key")) from None

    def host_keys(
        self,
        url: str,
        *,
        wait: bool = True,
        timeout: float = 60.0,
        on_update: Callable[[RepositoryProbe], None] | None = None,
    ) -> RepositoryProbe:
        """Fetch the SSH host keys of a repository URL's host; a worker connects, not the server.

        Needs `MANAGE_REPOSITORY`. Compare each key's `fingerprint` with your git host's
        published fingerprints before you put its `line` into `ssh_known_hosts`.

        Parameters
        ----------
        url
            An SSH repository URL (``git@host:path`` or ``ssh://…``).
        wait
            Poll until the check is done; `False` returns it as queued.
        timeout
            Seconds to keep polling.
        on_update
            Called with the check after each poll that found it not done yet.

        Returns
        -------
        RepositoryProbe
            The check; with its `host_keys` once done ``ok``.

        Raises
        ------
        lakana.errors.ValidationError
            If the URL is not a safe SSH URL.
        lakana.errors.ServerError
            With status 503 when no worker that takes pipeline work is running.
        TimeoutError
            If the check is not done after `timeout` seconds.
        """
        response = self._transport.request(
            ROUTES["repositories.probe"], json={"kind": "host_keys", "url": url}
        )
        probe = _probe_from_json(_object(response, "a repository check"))
        return self._wait(probe, timeout, on_update) if wait else probe

    def test_access(
        self,
        name: str,
        environment: str,
        *,
        wait: bool = True,
        timeout: float = 120.0,
        on_update: Callable[[RepositoryProbe], None] | None = None,
    ) -> RepositoryProbe:
        """Test that a worker can read a repository with an environment's credential.

        Needs `MANAGE_REPOSITORY` and `READ_ENVIRONMENT` on the environment. The worker runs
        `git ls-remote` with the repository's own known hosts; nothing is fetched.

        Parameters
        ----------
        name
            The repository's name.
        environment
            The environment whose secret of the repository's credential is used.
        wait
            Poll until the check is done; `False` returns it as queued.
        timeout
            Seconds to keep polling.
        on_update
            Called with the check after each poll that found it not done yet.

        Returns
        -------
        RepositoryProbe
            The check.

        Raises
        ------
        lakana.errors.NotFound
            If the repository or the environment does not exist.
        TimeoutError
            If the check is not done after `timeout` seconds.
        """
        response = self._transport.request(
            ROUTES["repositories.probe"],
            json={"kind": "access", "repository": name, "environment": environment},
        )
        probe = _probe_from_json(_object(response, "a repository check"))
        return self._wait(probe, timeout, on_update) if wait else probe

    def probe(self, probe_id: str) -> RepositoryProbe:
        """Show one of your repository checks (they are kept for 24 hours).

        Parameters
        ----------
        probe_id
            The check's id.

        Returns
        -------
        RepositoryProbe
            The check.

        Raises
        ------
        lakana.errors.NotFound
            If you have no check of that id.
        """
        response = self._transport.request(
            ROUTES["repositories.probe_get"], path_params={"probe_id": probe_id}
        )
        return _probe_from_json(_object(response, "a repository check"))

    def _wait(
        self,
        probe: RepositoryProbe,
        timeout: float,
        on_update: Callable[[RepositoryProbe], None] | None,
    ) -> RepositoryProbe:
        """Poll a check with a growing pause (0.5 s up to 2 s) until it is done.

        Parameters
        ----------
        probe
            The check as queued.
        timeout
            Seconds to keep polling.
        on_update
            Called with the check after each poll that found it not done yet.

        Returns
        -------
        RepositoryProbe
            The check, done.

        Raises
        ------
        TimeoutError
            If it is not done after `timeout` seconds.
        """
        deadline = _monotonic() + timeout
        interval = 0.5
        while probe.state != "done":
            if on_update is not None:
                on_update(probe)
            if _monotonic() >= deadline:
                raise TimeoutError(
                    f"The repository check is still {probe.state} after {timeout:.0f} s: "
                    + (
                        "no worker has taken it yet. Check that a worker that takes pipeline "
                        "work is running (`docker compose ps worker`, or the `lakana local` "
                        "terminal)."
                        if probe.state == "queued"
                        else "a worker is running it; its log has the details "
                        "(`docker compose logs worker`)."
                    )
                )
            _sleep(interval)
            interval = min(interval * 2, 2.0)
            probe = self.probe(probe.id)
        return probe
