"""The `configuration` resource: `Client.configuration`."""

from __future__ import annotations

from lakana._http import HttpTransport
from lakana._models import ConfiguredSetting
from lakana._routes import ROUTES

_SettingList = list[ConfiguredSetting]


class Configuration:
    """The server's configuration, read-only.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(self) -> _SettingList:
        """List the running server's settings. Needs the ADMIN role.

        Parameters
        ----------
        None
            Takes no parameters.

        Returns
        -------
        list of ConfiguredSetting
            One entry per setting, in the settings reference's order. A secret has no value:
            ``value`` is ``None`` and ``has_value`` says whether it is set.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you do not hold the ADMIN role on the instance with a personal credential.
        """
        response = self._transport.request(ROUTES["configuration.list"])
        return [
            ConfiguredSetting(
                name=item["name"],
                value=item["value"],
                secret=item["secret"],
                has_value=item["has_value"],
                source=item["source"],
            )
            for item in response.json()
        ]
