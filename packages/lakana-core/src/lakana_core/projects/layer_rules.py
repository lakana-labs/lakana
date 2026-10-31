"""Layer rules at deploy: every asset checked against the rules of its layer (#252).

Pure. The check reads the parsed assets and the declared rules and nothing else: no catalog
lookup, no principal. So whether an issue appears never depends on whether a table exists or may
be read, and every name in a message is one the deployer wrote. A read issue still carries its
target and a `withheld` line, because a failed deploy's error reaches every reader of the
environment (`error_for_reader`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from lakana_core.projects.asset_model import ParsedAsset, normalized_write
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.layers import (
    KINDS_NONE,
    LAYERS,
    SOURCE_LAYER,
    LayerRules,
    LayersConfig,
    and_list,
    layer_of,
    or_list,
)
from lakana_core.projects.parse import file_references

_EVERY_LAYER = and_list(LAYERS)


def asset_write(asset: ParsedAsset) -> str:
    """Say how an asset stores its result, in the words of `layers.<layer>.write`.

    Parameters
    ----------
    asset
        The parsed asset.

    Returns
    -------
    str
        ``view`` for `materialize: view`; otherwise `normalized_write`'s mode, so
        `materialize: incremental` is ``append`` and an asset that says nothing ``replace``.
    """
    if asset.metadata.materialize == "view":
        return "view"
    return normalized_write(asset.metadata)[0]


def _key(asset: ParsedAsset, key: str) -> str:
    """Name a header key the way the asset's language writes it.

    Parameters
    ----------
    asset
        The asset.
    key
        The key, such as ``kind``.

    Returns
    -------
    str
        The key in backticks, followed by a colon for SQL (``kind:``) or by an equals sign
        for Python (``kind=``).
    """
    return f"`{key}=`" if asset.kind == "python" else f"`{key}:`"


def _home(asset: ParsedAsset) -> str:
    """Name where the asset's keys are written.

    Parameters
    ----------
    asset
        The asset.

    Returns
    -------
    str
        ``header`` for SQL, ``decorator`` for Python.
    """
    return "decorator" if asset.kind == "python" else "header"


def _spelled(asset: ParsedAsset, key: str, value: str) -> str:
    """Write a key and its value the way the asset's language writes them.

    Parameters
    ----------
    asset
        The asset.
    key
        The key.
    value
        Its value.

    Returns
    -------
    str
        Both in backticks: ``kind: staging`` for SQL, ``kind="staging"`` for Python.
    """
    return f'`{key}="{value}"`' if asset.kind == "python" else f"`{key}: {value}`"


@dataclass(frozen=True)
class _Read:
    """One read of an asset that the rules judge.

    Parameters
    ----------
    name
        The name as the asset wrote it.
    targets
        The tables it may name: itself, and for a long-form reference also the table without
        its last part (``a.b.c`` is the table ``a.b.c`` or the column ``c`` of ``a.b``).
    line
        The line of the issue.
    reference
        Whether it comes from `references:`.
    parameter
        The Python parameter that reads it, or `None`.
    """

    name: str
    targets: tuple[str, ...]
    line: int
    reference: bool = False
    parameter: str | None = None

    def phrase(self, asset: ParsedAsset) -> str:
        """Say how the asset reads it.

        Parameters
        ----------
        asset
            The reading asset.

        Returns
        -------
        str
            "reads" and the name; with "(input" and the parameter for a Python input; or
            "references", the name and the `references` key for a reference.
        """
        if self.reference:
            return f"references `{self.name}` ({_key(asset, 'references')})"
        if self.parameter is not None:
            return f"reads `{self.name}` (input `{self.parameter}`)"
        return f"reads `{self.name}`"


def _reference_tables(target: str) -> tuple[str, ...]:
    """List the tables a `references:` target may name.

    Parameters
    ----------
    target
        The target as written: ``ns.table`` or ``ns.table.column``.

    Returns
    -------
    tuple of str
        `target`; for three or more parts also `target` without its last part.
    """
    if target.count(".") < 2:
        return (target,)
    return (target, target.rpartition(".")[0])


def _reads(asset: ParsedAsset) -> list[_Read]:
    """List the reads of an asset that the rules judge, by name.

    Parameters
    ----------
    asset
        The asset.

    Returns
    -------
    list of _Read
        Its SQL tables (at the body) or Python inputs (at the decorator, the first parameter
        of a table), then each `references:` target not already read (at the block), sorted by
        name. Left out: file reads and unqualified names, which the deploy already refuses as
        not found, and the asset's own table.
    """
    block = asset.block_line or asset.body_line
    reads: list[_Read] = []
    claimed: set[str] = set()
    if asset.kind == "python":
        for parameter, table in asset.inputs:
            if table not in claimed:
                claimed.add(table)
                reads.append(_Read(table, (table,), block, parameter=parameter))
    else:
        files = file_references(asset)
        for table in asset.references:
            if table not in files:
                claimed.add(table)
                reads.append(_Read(table, (table,), asset.body_line))
    for _, target in sorted(asset.metadata.references.items()):
        tables = _reference_tables(target)
        if claimed.isdisjoint(tables):
            claimed.update(tables)
            reads.append(_Read(target, tables, block, reference=True))
    kept = [r for r in reads if "." in r.name and asset.table not in r.targets]
    return sorted(kept, key=lambda read: read.name)


def _read_text(asset: ParsedAsset, layer: str, rules: LayerRules, read: _Read) -> str | None:
    """Judge one read against the asset's layer.

    Parameters
    ----------
    asset
        The reading asset.
    layer
        Its layer.
    rules
        Its layer's rules.
    read
        The read.

    Returns
    -------
    str or None
        The message, or `None` when the read is allowed.
    """
    head = f"`{asset.table}` {read.phrase(asset)}"
    target = layer_of(read.name)
    if target is None:
        if layer == SOURCE_LAYER:
            return None
        then = "point the reference at" if read.reference else "read"
        return (
            f"{head}, which is outside {_EVERY_LAYER}, so it is a source, and only {SOURCE_LAYER} "
            f"reads sources (layers.{layer}.reads). Load it into {SOURCE_LAYER} first with a "
            f"{SOURCE_LAYER} asset, then {then} the {SOURCE_LAYER} table"
        )
    allowed = rules.reads
    if target in allowed:
        return None
    add = f"add {target} to layers.{layer}.reads in lakana.yaml"
    if not allowed:
        what = "only sources" if layer == SOURCE_LAYER else "no table"
        return (
            f"{head}, which is in {target}, but layers.{layer}.reads lists no layer, so {layer} "
            f"reads {what}. Build this table in {target}, or {add}"
        )
    fix = (
        f"Point the reference at a {or_list(allowed)} table"
        if read.reference
        else f"Read the data from {or_list(allowed)} instead"
    )
    return (
        f"{head}, which is in {target}, but layers.{layer}.reads allows only "
        f"{and_list(allowed)}. {fix}, or {add}"
    )


def _write_text(asset: ParsedAsset, layer: str, rules: LayerRules) -> str | None:
    """Judge how the asset stores its result.

    Parameters
    ----------
    asset
        The asset, not an artifact.
    layer
        Its layer.
    rules
        Its layer's rules.

    Returns
    -------
    str or None
        The message, or `None` when its write is listed.
    """
    allowed = rules.write
    if not allowed:
        return (
            f"`{asset.table}` is in {layer}, but layers.{layer}.write is empty, so this project "
            f"builds no {layer} asset. Move the asset to another layer, or list its write mode in "
            f"layers.{layer}.write in lakana.yaml"
        )
    write = asset_write(asset)
    if write in allowed:
        return None
    if write == "view":
        return (
            f"`{asset.table}` is a view (`materialize: view`), but layers.{layer}.write allows "
            f"only {and_list(allowed)}. Build it as a table, or add view to layers.{layer}.write "
            "in lakana.yaml"
        )
    those = "one of those" if len(allowed) > 1 else allowed[0]
    return (
        f"`{asset.table}` is written with `{write}`, but layers.{layer}.write allows only "
        f"{and_list(allowed)}. Write it with {those} ({_key(asset, 'write')} in its "
        f"{_home(asset)}), or add {write} to layers.{layer}.write in lakana.yaml"
    )


def _kind_text(asset: ParsedAsset, layer: str, rules: LayerRules) -> str | None:
    """Judge the asset's `kind:`.

    Parameters
    ----------
    asset
        The asset, not an artifact.
    layer
        Its layer.
    rules
        Its layer's rules.

    Returns
    -------
    str or None
        The message, or `None` when its kind (or its lack of one) is what the layer asks.
    """
    kind, kinds = asset.metadata.kind, rules.kinds
    if kinds == KINDS_NONE:
        if kind is None:
            return None
        return (
            f"`{asset.table}` declares {_spelled(asset, 'kind', kind)}, but layers.{layer}.kinds "
            f"is none, so a {layer} asset declares no kind. Remove {_key(asset, 'kind')} from its "
            f"{_home(asset)}"
        )
    listed = ", ".join(kinds)
    if kind is None:
        need = f"one of {listed}" if len(kinds) > 1 else kinds[0]
        return (
            f"`{asset.table}` declares no kind, but layers.{layer}.kinds needs every {layer} "
            f"asset to declare {need}. Add {_key(asset, 'kind')} to its {_home(asset)}"
        )
    if kind in kinds:
        return None
    other = "one of those kinds" if len(kinds) > 1 else _spelled(asset, "kind", kinds[0])
    return (
        f"`{asset.table}` declares {_spelled(asset, 'kind', kind)}, but layers.{layer}.kinds "
        f"allows only {listed}. Move it to the layer for {kind} tables, or declare {other}"
    )


def _outside_text(asset: ParsedAsset) -> str:
    """Refuse an asset whose table is in no layer.

    Parameters
    ----------
    asset
        The asset; only an artifact can be outside, since the catalog keeps every table and view
        in a layer.

    Returns
    -------
    str
        The message, with the file and name the asset would have in gold.
    """
    gold = LAYERS[-1]
    file = PurePosixPath(asset.path).name
    name = asset.table.rpartition(".")[2]
    return (
        f"`{asset.table}` is outside {_EVERY_LAYER}, but a project with `layers:` builds every "
        f"asset in one of them. Move it to the layer whose assets read it, such as "
        f"assets/{gold}/{file} for {gold}.{name}"
    )


def _asset_issues(layers: LayersConfig, asset: ParsedAsset) -> list[ProjectIssue]:
    """Check one asset against its layer's rules.

    Parameters
    ----------
    layers
        The declared rules.
    asset
        The asset.

    Returns
    -------
    list of ProjectIssue
        Outside the layers: that one issue. Otherwise its write and kind issues (not for an
        artifact), then one per offending read (with `targets` and `withheld`), then one per
        connection outside bronze.
    """
    block = asset.block_line or asset.body_line
    layer = layer_of(asset.table)
    if layer is None:
        return [ProjectIssue(asset.path, block, _outside_text(asset))]
    rules = layers.for_layer(layer)
    texts = (
        []
        if asset.output == "artifact"
        else [_write_text(asset, layer, rules), _kind_text(asset, layer, rules)]
    )
    issues = [ProjectIssue(asset.path, block, text) for text in texts if text is not None]
    withheld = f"`{asset.table}` breaks layers.{layer}.reads with a read that is not shown to you"
    for read in _reads(asset):
        text = _read_text(asset, layer, rules, read)
        if text is not None:
            issues.append(
                ProjectIssue(asset.path, read.line, text, targets=read.targets, withheld=withheld)
            )
    if layer != SOURCE_LAYER:
        issues.extend(
            ProjectIssue(
                asset.path,
                block,
                f"`{asset.table}` declares connection `{connection}`, a source, and only "
                f"{SOURCE_LAYER} reads sources (layers.{layer}.reads). Move the asset to "
                f"assets/{SOURCE_LAYER}/, then build {layer} from the {SOURCE_LAYER} table",
            )
            for connection in asset.connections
        )
    return issues


def layer_issues(layers: LayersConfig | None, assets: Sequence[ParsedAsset]) -> list[ProjectIssue]:
    """Check every asset against the rules of its layer.

    Parameters
    ----------
    layers
        `ProjectConfig.layers`; `None` (no `layers:`) checks nothing.
    assets
        The parsed assets. User-written checks are not passed: they produce no data, so they
        may read any layer.

    Returns
    -------
    list of ProjectIssue
        By asset path: an asset outside the layers, or its write, kind, read (by name) and
        connection issues.
    """
    if layers is None:
        return []
    issues: list[ProjectIssue] = []
    for asset in sorted(assets, key=lambda a: a.path):
        issues.extend(_asset_issues(layers, asset))
    return issues
