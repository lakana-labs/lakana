"""`health:` in the asset header, the `@asset` decorator and `lakana.yaml` (#340).

The syntax is ``off`` (every signal off) or a mapping of signals to ``warn``, ``block`` or
``off``; ``null_rate`` and ``range`` may instead hold a mapping of single columns to an effect
(only in an asset, not in `lakana.yaml`). Every spelling is read into one flat map, ``signal`` or
``signal.column`` → effect, which is what the manifest stores. YAML reads an unquoted ``off`` as
``False``, so ``False`` means ``off`` here; ``on``, ``yes`` and ``true`` are refused.

`lakana_core.catalog` imports `projects.asset_model`, which imports `project_config`, which imports
this module for the `health:` validator; so the functions import the catalog's names themselves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from lakana_core.errors import ValidationError
from lakana_core.identifiers import validate_path_component
from lakana_core.projects.history_columns import history_table_columns
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.yaml_loader import shown_key

if TYPE_CHECKING:
    from lakana_core.catalog.health_signals import HealthEffects
    from lakana_core.projects.asset_model import ParsedAsset

NOT_A_SETTING = "takes off, or a mapping of signals to warn, block or off, such as {volume: block}"
UNKNOWN_SIGNAL = "{name} is not a health signal; use freshness, volume, null_rate, range or schema"
BAD_EFFECT = "{where} must be warn, block or off, not {value}"
ON_IS_NOT_AN_EFFECT = (
    "{where} must be warn, block or off; on, yes and true are not effects "
    "(to turn a signal off, write off)"
)
FRESHNESS_CANNOT_BLOCK = (
    "freshness cannot block: it fires between runs, when there is no write to stop; use warn or off"
)
TABLE_SIGNAL = "{signal} is about the whole table; give it warn, block or off, not columns"
NO_COLUMNS_HERE = (
    "{signal} takes warn, block or off in lakana.yaml; name single columns in an asset's header"
)
VIEW_HAS_NO_SNAPSHOTS = (
    "health: does not apply to a view, which has no snapshots to watch; remove it, "
    "or materialize the asset as a table"
)
UNKNOWN_COLUMN = (
    "health: {signal} names column {column}, which {table} does not have "
    "(columns: {columns}); fix the name or remove it"
)
PAST_THE_JUDGED_COLUMNS = (
    "health: {signal} of {column} can never block: only the first {most} columns of a table are "
    "judged, and {column} is column {position}; move it among the first {most}, or set it to warn "
    "or off"
)


def _effect(where: str, value: object) -> str:
    """Read one effect.

    Parameters
    ----------
    where
        The signal, or ``signal: column``, for the message.
    value
        The value; YAML reads an unquoted ``off`` as `False`.

    Returns
    -------
    str
        ``warn``, ``block`` or ``off``.

    Raises
    ------
    ValueError
        For anything else.
    """
    from lakana_core.catalog.health_signals import EFFECTS

    if value is False:
        return "off"
    if value is True:
        raise ValueError(ON_IS_NOT_AN_EFFECT.format(where=where))
    if isinstance(value, str) and value in EFFECTS:
        return value
    raise ValueError(BAD_EFFECT.format(where=where, value=shown_key(value)))


def normalize_health(value: object, *, columns: bool = True) -> dict[str, str]:
    """Read a `health:` value into the flat map of effects.

    Parameters
    ----------
    value
        What the header, the decorator or `lakana.yaml` says.
    columns
        Whether single columns may be named (not in `lakana.yaml`).

    Returns
    -------
    dict
        ``signal`` or ``signal.column`` → effect, sorted; every signal ``off`` for ``off``.

    Raises
    ------
    ValueError
        With one of the module's texts.
    """
    from lakana_core.catalog.health_signals import COLUMN_SIGNALS, SIGNALS

    if value is None:
        return {}
    if value is False or value == "off":
        return dict.fromkeys(SIGNALS, "off")
    if not isinstance(value, Mapping):
        raise ValueError(NOT_A_SETTING)
    flat: dict[str, str] = {}
    for signal, given in value.items():
        if signal not in SIGNALS:
            raise ValueError(UNKNOWN_SIGNAL.format(name=shown_key(signal)))
        if not isinstance(given, Mapping):
            flat[signal] = _effect(signal, given)
            continue
        if signal not in COLUMN_SIGNALS:
            raise ValueError(TABLE_SIGNAL.format(signal=signal))
        if not columns:
            raise ValueError(NO_COLUMNS_HERE.format(signal=signal))
        for column, effect in given.items():
            name = column if isinstance(column, str) else shown_key(column)
            try:
                validate_path_component(f"health_{signal}__{name}", what="Check name")
            except ValidationError as exc:
                raise ValueError(
                    f"column {shown_key(column)} cannot name its check "
                    f"health_{signal}__{shown_key(column)}: {exc}"
                ) from None
            flat[f"{signal}.{name}"] = _effect(f"{signal}: {name}", effect)
    if flat.get("freshness") == "block":
        raise ValueError(FRESHNESS_CANNOT_BLOCK)
    return dict(sorted(flat.items()))


def resolve_effects(project: Mapping[str, str], asset: Mapping[str, str]) -> dict[str, str]:
    """Lay an asset's `health:` over the project's and keep what differs from the defaults.

    Parameters
    ----------
    project
        `lakana.yaml`'s `health:`, flat.
    asset
        The asset's `health:`, flat.

    Returns
    -------
    dict
        The merged map without an entry that says what the default says: a table-level entry
        other than ``warn`` and a column entry other than its signal's table-level effect.
    """
    from lakana_core.catalog.health_signals import DEFAULT_EFFECT

    merged = {**project, **asset}
    kept: dict[str, str] = {}
    for key, effect in sorted(merged.items()):
        signal = key.partition(".")[0]
        default = merged.get(signal, DEFAULT_EFFECT) if key != signal else DEFAULT_EFFECT
        if effect != default:
            kept[key] = effect
    return kept


def entry_effects(entry: object) -> HealthEffects:
    """Read the effects a manifest asset entry stores.

    Parameters
    ----------
    entry
        A manifest asset entry; untrusted, like a stored manifest.

    Returns
    -------
    HealthEffects
        Its `health` entries that are valid; every signal warns when there are none.
    """
    from lakana_core.catalog.health_signals import HealthEffects

    if not isinstance(entry, Mapping):
        return HealthEffects.default()
    return HealthEffects.from_flat(entry.get("health"))


def health_issues(assets: Sequence[ParsedAsset]) -> list[ProjectIssue]:
    """Check each asset's `health:` against what the asset is.

    Parameters
    ----------
    assets
        The parsed assets.

    Returns
    -------
    list of ProjectIssue
        One per asset that is a view and has `health:`, one per column entry that names a
        column the table does not have (the exact name; a history table's added columns count,
        its `valid_from:` column does not), and one per ``block`` entry on a column past the
        `MAX_HEALTH_COLUMNS`-th, which no commit judges; columns are not checked for a star or a
        Python asset. In path order.
    """
    from lakana_core.catalog.health_signals import MAX_HEALTH_COLUMNS

    issues: list[ProjectIssue] = []
    for asset in sorted(assets, key=lambda a: a.path):
        health = asset.metadata.health
        if not health:
            continue
        line = asset.block_line or asset.body_line
        if asset.metadata.materialize == "view":
            issues.append(ProjectIssue(asset.path, line, VIEW_HAS_NO_SNAPSHOTS))
            continue
        if asset.columns is None:
            continue
        has = asset.columns
        if asset.metadata.write == "history":
            has = history_table_columns(asset.table, has, asset.metadata.valid_from)
        issues.extend(
            ProjectIssue(
                asset.path,
                line,
                UNKNOWN_COLUMN.format(
                    signal=signal,
                    column=shown_key(column),
                    table=asset.table,
                    columns=", ".join(shown_key(c) for c in has),
                ),
            )
            for signal, _, column in (key.partition(".") for key in health if "." in key)
            if column not in has
        )
        issues.extend(
            ProjectIssue(
                asset.path,
                line,
                PAST_THE_JUDGED_COLUMNS.format(
                    signal=signal,
                    column=shown_key(column),
                    most=MAX_HEALTH_COLUMNS,
                    position=has.index(column) + 1,
                ),
            )
            for key, effect in health.items()
            for signal, _, column in (key.partition("."),)
            if effect == "block" and column in has[MAX_HEALTH_COLUMNS:]
        )
    return issues
