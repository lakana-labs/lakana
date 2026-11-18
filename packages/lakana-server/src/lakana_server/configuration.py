"""The server's configuration as the settings area shows it: one entry per setting, no secret value.

Read-only. `describe_settings` walks the settings tree, so a setting added to the models appears
here, in declaration order, without a change to this module. A secret is recognised by its
annotation (`SecretStr`) or by name (`PLAIN_SECRETS`), and its value never leaves this module.

The walk follows the annotations of the model classes, never the values of one instance: a field
is either a plain nested model (descended into) or a leaf built only from known scalars
(`_SCALARS`, `Literal`, an enum, and lists, tuples, sets, dicts and unions of them). Any other
shape, such as an optional or listed model, is refused with `TypeError`, because its fields
would be printed under a name nobody reviewed. So a new credential cannot reach this endpoint
without passing the review in `tests/test_configuration.py`.
"""

from __future__ import annotations

import enum
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, JsonValue, SecretStr, TypeAdapter
from pydantic.fields import FieldInfo

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


_SCALARS: frozenset[object] = frozenset({str, int, float, bool, type(None), Path, SecretStr})
"""The leaf types a setting may be built from; anything else is refused by `_known_leaf`."""

_CONTAINERS: frozenset[object] = frozenset(
    {list, tuple, set, frozenset, dict, typing.Union, types.UnionType}
)
"""The generic origins whose arguments `_known_leaf` checks in turn."""


def _variable(path: tuple[str, ...]) -> str:
    """Name the environment variable of a setting.

    Parameters
    ----------
    path
        The field names from the root to the setting.

    Returns
    -------
    str
        Such as ``LAKANA_RUN__LEASE_SECONDS``.
    """
    return "LAKANA_" + "__".join(path).upper()


def _plain_model(annotation: object) -> type[BaseModel] | None:
    """Return the model class an annotation names, if it is exactly one model.

    Parameters
    ----------
    annotation
        A field's annotation.

    Returns
    -------
    type of BaseModel or None
        The class for a plain nested model; `None` for anything else.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _known_leaf(annotation: object) -> bool:
    """Tell whether an annotation is built only from known scalars.

    Parameters
    ----------
    annotation
        A field's annotation, or one of its arguments.

    Returns
    -------
    bool
        True for a type of `_SCALARS`, an enum, a `Literal`, and a list, tuple, set, dict or
        union whose arguments are all known leaves; False for a model anywhere inside, and for
        every other type.
    """
    if annotation in _SCALARS or annotation is Ellipsis:
        return True
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return True
    origin = typing.get_origin(annotation)
    if origin is Literal:
        return True
    if origin is typing.Annotated:
        return _known_leaf(typing.get_args(annotation)[0])
    if origin in _CONTAINERS or isinstance(annotation, types.UnionType):
        return all(_known_leaf(arg) for arg in typing.get_args(annotation))
    return False


def _fields(
    model: type[BaseModel], path: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], FieldInfo | None]]:
    """Yield every field of a model class and its nested model classes, in declaration order.

    Parameters
    ----------
    model
        The settings class or a nested one.
    path
        The field names from the root to `model`.

    Yields
    ------
    tuple of (tuple of str, FieldInfo or None)
        The path of a field, with the field for a leaf and `None` for a nested model, which
        is yielded before its own fields.

    Raises
    ------
    TypeError
        For a field that is neither a plain nested model nor a known leaf (`_known_leaf`).
    """
    for name, field in model.model_fields.items():
        here = (*path, name)
        nested = _plain_model(field.annotation)
        if nested is not None:
            yield here, None
            yield from _fields(nested, here)
        elif _known_leaf(field.annotation):
            yield here, field
        else:
            raise TypeError(
                f"{_variable(here)} has the type {field.annotation!r}: a setting must be a "
                "plain nested model or built from str, int, float, bool, Path, SecretStr, "
                "Literal or an enum, so that the configuration endpoint cannot print a "
                "credential nobody reviewed. Make it one of those, or extend "
                "lakana_server.configuration and its review in tests/test_configuration.py."
            )


def setting_names(model: type[BaseModel]) -> list[str]:
    """List the environment variable of every leaf setting of a settings class.

    Read from the annotations of the classes, so a nested model that a default instance
    leaves unset is listed too.

    Parameters
    ----------
    model
        The settings class, such as `lakana_server.settings.Settings`.

    Returns
    -------
    list of str
        The variables, in declaration order.

    Raises
    ------
    TypeError
        For a field that is neither a plain nested model nor built from known scalars.
    """
    return [_variable(path) for path, field in _fields(model, ()) if field is not None]


def _walk(
    model: BaseModel, path: tuple[str, ...], explicit: bool, found: list[ConfiguredSetting]
) -> None:
    """Add the leaf settings of one model, descending into nested models.

    Parameters
    ----------
    model
        The settings model or a nested one, whose class `_fields` has already accepted.
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
        if _plain_model(field.annotation) is not None:
            _walk(value, here, given, found)
            continue
        variable = _variable(here)
        secret = _holds_secret_str(field.annotation) or variable in PLAIN_SECRETS
        found.append(
            ConfiguredSetting(
                name=variable,
                value=None
                if secret
                else TypeAdapter(field.annotation or Any).dump_python(value, mode="json"),
                secret=secret,
                has_value=value is not None,
                source="set" if given else "default",
            )
        )


def describe_settings(settings: BaseModel) -> list[ConfiguredSetting]:
    """List every setting of the server with where it came from, masking each secret.

    Parameters
    ----------
    settings
        The running server's settings (`lakana_server.settings.Settings`).

    Returns
    -------
    list of ConfiguredSetting
        One entry per leaf setting, in the declaration order of `Settings`. A secret has no
        value, only `has_value`.

    Raises
    ------
    TypeError
        If a field of the settings classes is neither a plain nested model nor built from
        known scalars (`setting_names`); checked before any value is read.
    """
    setting_names(type(settings))
    found: list[ConfiguredSetting] = []
    _walk(settings, (), True, found)
    return found
