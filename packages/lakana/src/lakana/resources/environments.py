"""The `environments` resource: `Client.environments`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import (
    Environment,
    EnvironmentUsage,
    OrphanedLocation,
    OrphanedLocations,
    UsagePart,
    known_fields,
)
from lakana._routes import ROUTES


def _environment_from_json(data: dict[str, Any]) -> Environment:
    """Build an `Environment` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `EnvironmentOut` JSON body.

    Returns
    -------
    Environment
        The parsed environment.
    """
    return Environment(
        name=data["name"],
        storage_root=data["storage_root"],
        storage_secret_ref=data["storage_secret_ref"],
        is_protected=data["is_protected"],
        properties=data["properties"],
        retention_days=data.get("retention_days"),
        deleted_at=_moment(data.get("deleted_at")),
        purge_after=_moment(data.get("purge_after")),
    )


def _moment(value: str | None) -> datetime | None:
    """Read an optional ISO 8601 time.

    Parameters
    ----------
    value
        The JSON value; absent from a server older than environment deletion.

    Returns
    -------
    datetime or None
        The time.
    """
    return None if value is None else datetime.fromisoformat(value)


def _orphaned_from_json(data: dict[str, Any]) -> OrphanedLocations:
    """Build an `OrphanedLocations` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `OrphanedLocationsOut` JSON body.

    Returns
    -------
    OrphanedLocations
        The parsed report.
    """
    return OrphanedLocations(
        environment=data["environment"],
        older_than_days=data["older_than_days"],
        complete=data["complete"],
        locations=[
            OrphanedLocation(
                location=item["location"],
                files=item["files"],
                bytes=item["bytes"],
                newest_file_at=_moment(item["newest_file_at"]),
                recent=item["recent"],
                deleted=item["deleted"],
                files_deleted=item["files_deleted"],
                bytes_deleted=item["bytes_deleted"],
                kept_because=item["kept_because"],
            )
            for item in data["locations"]
        ],
    )


_EnvironmentList = list[Environment]
"""Alias for `list[Environment]`, resolved at module scope.

`Environments` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class Environments:
    """The `environments` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, storage: dict[str, Any]) -> Environment:
        """Create an environment.

        Parameters
        ----------
        name
            The environment's unique name.
        storage
            Storage configuration: `{"scheme", "root", "options"}`. The root is in the
            scheme `scheme` names: `file:///data/lakana` for `file`, `s3://bucket/lakana`
            for `s3`, `abfs://container/lakana` for `abfs`.

        Returns
        -------
        Environment
            The created environment.

        Raises
        ------
        lakana.errors.Conflict
            If an environment of this name already exists.
        lakana.errors.ValidationError
            If the root is in another scheme than `scheme`, or is refused for another reason;
            the message names the root to use.
        """
        response = self._transport.request(
            ROUTES["environments.create"], json={"name": name, "storage": storage}
        )
        return _environment_from_json(response.json())

    def get(self, name: str) -> Environment:
        """Fetch an environment by name.

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        Environment
            The environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        """
        response = self._transport.request(ROUTES["environments.get"], path_params={"name": name})
        return _environment_from_json(response.json())

    def list(self, *, deleted: bool = False) -> _EnvironmentList:
        """List every environment the caller may read.

        Parameters
        ----------
        deleted
            List the deleted environments whose files are not deleted yet instead, each with
            its `purge_after`.

        Returns
        -------
        list of Environment
            Every readable environment.
        """
        params = {"deleted": "true"} if deleted else None
        response = self._transport.request(ROUTES["environments.list"], params=params)
        return [_environment_from_json(item) for item in response.json()]

    def delete(self, name: str, *, purge: bool = False) -> Environment | None:
        """Delete an environment: its tables can be restored with `restore` until `purge_after`.

        Every managed table becomes a dropped table whose files the server's clean-up deletes
        after the undo period (`LAKANA_TABLES__UNDO_DAYS`, 7 days by default); views and
        external tables are removed, and an external table's files are never deleted.

        Parameters
        ----------
        name
            The environment's name.
        purge
            Make everything due at once: the next clean-up deletes the files, and the
            environment cannot be restored. Also accepted for an environment already deleted.

        Returns
        -------
        Environment or None
            The deleted environment, with `deleted_at` and `purge_after`; `None` from a server
            older than environment restore, which kept the files for ever.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists, or it is already deleted and `purge` is
            false.
        lakana.errors.Conflict
            If a repository is still deployed to it; undeploy first.
        lakana.errors.ValidationError
            If it is protected.
        """
        response = self._transport.request(
            ROUTES["environments.delete"],
            path_params={"name": name},
            params={"purge": "true"} if purge else None,
        )
        if response.status_code == 204 or not response.content:
            return None
        return _environment_from_json(response.json())

    def restore(self, name: str) -> Environment:
        """Restore a deleted environment within its undo period, with the tables it dropped.

        The tables come back with the same ids, data and grants; a table dropped before the
        environment was deleted stays dropped (`Tables.undrop` restores it).

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        Environment
            The restored environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        lakana.errors.Conflict
            If it is not deleted, its undo period has ended, or the clean-up has started
            deleting its files.
        """
        response = self._transport.request(
            ROUTES["environments.restore"], path_params={"name": name}
        )
        return _environment_from_json(response.json())

    def update_storage(
        self, name: str, options: dict[str, str] | None = None, secret_ref: str | None = None
    ) -> Environment:
        """Update an environment's storage options and/or secret reference.

        Parameters
        ----------
        name
            The environment's name.
        options
            Replaces the environment's storage options entirely, if given.
        secret_ref
            Replaces the environment's storage secret reference, if given.

        Returns
        -------
        Environment
            The updated environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        """
        response = self._transport.request(
            ROUTES["environments.update_storage"],
            path_params={"name": name},
            json={"options": options, "secret_ref": secret_ref},
        )
        return _environment_from_json(response.json())

    def set_protection(self, name: str, protected: bool) -> Environment:
        """Toggle an environment's protected flag.

        Parameters
        ----------
        name
            The environment's name.
        protected
            The new `is_protected` value.

        Returns
        -------
        Environment
            The updated environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        """
        response = self._transport.request(
            ROUTES["environments.set_protection"],
            path_params={"name": name},
            json={"is_protected": protected},
        )
        return _environment_from_json(response.json())

    def set_retention(self, name: str, days: int | None) -> Environment:
        """Set the retention of an environment's tables that set none of their own.

        A table then expires `days` after its last activity (the later of its last change and
        its last recorded read), and never earlier than `days` after this call. Tables in a
        protected environment never expire. Needs `MANAGE_ENVIRONMENT` on the environment.

        Parameters
        ----------
        name
            The environment's name.
        days
            1 to 3,650 days, or `None` to turn the retention off.

        Returns
        -------
        Environment
            The environment.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        lakana.errors.ValidationError
            If `days` is out of range, or the environment is protected.
        """
        response = self._transport.request(
            ROUTES["environments.set_retention"], path_params={"name": name}, json={"days": days}
        )
        return _environment_from_json(response.json())

    def usage(self, name: str) -> EnvironmentUsage:
        """Report how much storage an environment uses.

        The server adds up what the catalog database holds and reads nothing from object
        storage: the sizes of managed tables are the ones the worker's hourly sweep measured,
        so a table written since counts as `unmeasured` until the next sweep. Needs
        `MANAGE_ENVIRONMENT` on the environment.

        Parameters
        ----------
        name
            The environment's name.

        Returns
        -------
        EnvironmentUsage
            Its tables, dropped tables, artifacts and code archives.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        lakana.errors.PermissionDenied
            Without `MANAGE_ENVIRONMENT` on the environment.
        """
        data = self._transport.request(
            ROUTES["environments.usage"], path_params={"name": name}
        ).json()
        return EnvironmentUsage(
            environment=data["environment"],
            tables=UsagePart(**known_fields(UsagePart, data["tables"])),
            dropped_tables=UsagePart(**known_fields(UsagePart, data["dropped_tables"])),
            artifacts=UsagePart(**known_fields(UsagePart, data["artifacts"])),
            code_archives=UsagePart(**known_fields(UsagePart, data["code_archives"])),
            total_bytes=data["total_bytes"],
            complete=data["complete"],
        )

    def orphaned_locations(self, name: str, *, older_than_days: int = 30) -> OrphanedLocations:
        """List the table directories older releases left without a location record.

        A dry run: nothing is deleted. Needs `MANAGE_ENVIRONMENT` on the instance; a deleted
        environment is accepted.

        Parameters
        ----------
        name
            The environment's name.
        older_than_days
            A directory with a file newer than this many days (1 to 3,650) is listed as recent
            and is never deleted.

        Returns
        -------
        OrphanedLocations
            The directories; `complete` is false when the server's time limit cut the listing.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        lakana.errors.ValidationError
            If `older_than_days` is out of range.
        """
        response = self._transport.request(
            ROUTES["environments.orphaned_locations"],
            path_params={"name": name},
            params={"older_than_days": str(older_than_days)},
        )
        return _orphaned_from_json(response.json())

    def delete_orphaned_locations(
        self, name: str, *, older_than_days: int = 30
    ) -> OrphanedLocations:
        """Delete the table directories without a location record whose files are old enough.

        Each directory is checked again first; one that a table, a create or a dropped table
        now uses is kept, with the reason. Audited. Call again while `complete` is false.

        Parameters
        ----------
        name
            The environment's name.
        older_than_days
            Only directories whose newest file is older than this many days are deleted.

        Returns
        -------
        OrphanedLocations
            Each directory, deleted or with the reason it was kept.

        Raises
        ------
        lakana.errors.NotFound
            If no environment of this name exists.
        lakana.errors.ValidationError
            If `older_than_days` is out of range.
        """
        response = self._transport.request(
            ROUTES["environments.delete_orphaned_locations"],
            path_params={"name": name},
            json={"older_than_days": older_than_days},
        )
        return _orphaned_from_json(response.json())
