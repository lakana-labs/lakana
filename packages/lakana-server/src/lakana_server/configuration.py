"""The server's configuration as the settings area shows it: one entry per setting, no secret value.

Read-only. `describe_settings` walks the settings tree, so a setting added to the models appears
here, in declaration order, without a change to this module. A secret is recognised by its
annotation (`SecretStr`) or by name (`PLAIN_SECRETS`), and its value never leaves this module.
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, JsonValue, SecretStr

from lakana_server.settings import Settings

PLAIN_SECRETS: frozenset[str] = frozenset({"LAKANA_DATABASE__URL"})
"""Secret settings that are plain `str` in the models: the database URL carries its password."""


@dataclass(frozen=True)
class ConfiguredSetting:
    """One setting of the running server.

    Parameters
    ----------
    name
        The environment variable that sets it, such as `LAKANA_RUN__LEASE_SECONDS`.
    value
        The value in its JSON form; always `None` for a secret.
    secret
        Whether the setting holds a credential. Its value is never shown.
    has_value
        Whether the setting holds a value (not `None`). For a secret, this is "set" or "not set".
    source
        `"set"` when the environment or the `.env` file gave it (also when it equals the default),
        else `"default"`.
    """

    name: str
    value: JsonValue
    secret: bool
    has_value: bool
    source: Literal["set", "default"]


def _holds_secret_str(annotation: object) -> bool:
    """Tell whether an annotation is or contains `SecretStr`.

    Parameters
    ----------
    annotation
        A field's annotation.

    Returns
    -------
    bool
        True for `SecretStr`, and for a union, list or other generic that names it.
    """
    if annotation is SecretStr:
        return True
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is not None:
        return any(_holds_secret_str(arg) for arg in typing.get_args(annotation))
    return False


def _walk(
    model: BaseModel, path: tuple[str, ...], explicit: bool, found: list[ConfiguredSetting]
) -> None:
    """Add the leaf settings of one model, descending into nested models.

    Parameters
    ----------
    model
        The settings model or a nested one.
    path
        The field names from the root to `model`.
    explicit
        Whether every step of `path` was set explicitly.
    found
        The list to add to, in declaration order.
    """
    for name, field in type(model).model_fields.items():
        value: Any = getattr(model, name)
        here = (*path, name)
        given = explicit and name in model.model_fields_set
        if isinstance(value, BaseModel):
            _walk(value, here, given, found)
            continue
        variable = "LAKANA_" + "__".join(here).upper()
        secret = _holds_secret_str(field.annotation) or variable in PLAIN_SECRETS
        found.append(
            ConfiguredSetting(
                name=variable,
                value=None if secret else model.model_dump(mode="json", include={name})[name],
                secret=secret,
                has_value=value is not None,
                source="set" if given else "default",
            )
        )


def describe_settings(settings: Settings) -> list[ConfiguredSetting]:
    """List every setting of the server with where it came from, masking each secret.

    Parameters
    ----------
    settings
        The running server's settings.

    Returns
    -------
    list of ConfiguredSetting
        One entry per leaf setting, in the declaration order of `Settings`. A secret has no
        value, only `has_value`.
    """
    found: list[ConfiguredSetting] = []
    _walk(settings, (), True, found)
    return found
