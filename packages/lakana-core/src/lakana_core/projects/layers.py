"""Medallion layers: the three names, `lakana.yaml`'s `layers:` section and its default (#252).

The layers are fixed: `bronze`, `silver` and `gold`, each the top-level namespace of the same
name, so a table's layer is the first part of its name (`layer_of`). `layers:` declares only the
rules of the three: which layers each may read, which write modes and which model kinds its
assets use. The deploy check is `layer_rules`.

A leaf of `projects`: `project_config` imports it for `ProjectConfig.layers`, so it imports
nothing that imports `project_config`. That is why the vocabulary comes from `vocabulary` and the
two models repeat `StrictModel`'s configuration instead of inheriting it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.vocabulary import MODEL_KINDS, WRITE_CHOICES
from lakana_core.projects.yaml_loader import key_line, shown_key

LAYERS = ("bronze", "silver", "gold")
"""The layers, in the order data flows through them; each is the top-level namespace of its
name. The catalog's `_MEDALLION_TOP_LEVEL_NAMESPACES` and `loads.LOAD_LAYER` are built from it."""

SOURCE_LAYER = LAYERS[0]
"""The one layer that reads sources: tables outside the layers, and connections (Q3)."""

LAYER_WRITES = (*WRITE_CHOICES, "view")
"""What `layers.<layer>.write` may list: the write modes, and ``view`` for `materialize: view`.
It grows with `WRITE_CHOICES` (#253 adds ``history`` there)."""

RULES = ("reads", "write", "kinds")
"""The rules every layer declares, in the order the texts name them."""

KINDS_NONE = "none"
"""`kinds: none`: no asset of the layer may declare `kind:`."""

_LIST_KINDS = "list the kinds this layer's assets may declare, or write `none` if they declare none"
_KINDS_EXAMPLE = "`[fact, dimension, wide]`"


def and_list(items: Sequence[str]) -> str:
    """Join words as ``a, b and c``.

    Parameters
    ----------
    items
        The words, in order; at least one.

    Returns
    -------
    str
        The one word, or all but the last joined by commas and the last by ``and``.
    """
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def or_list(items: Sequence[str]) -> str:
    """Join words as ``a, b or c``.

    Parameters
    ----------
    items
        The words, in order; at least one.

    Returns
    -------
    str
        The one word, or all but the last joined by commas and the last by ``or``.
    """
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} or {items[-1]}"


def flow_value(value: str | Sequence[str]) -> str:
    """Render a rule's value in YAML flow style.

    Parameters
    ----------
    value
        ``none``, or the listed values.

    Returns
    -------
    str
        ``none`` as it is, a list as ``[a, b]``.
    """
    return value if isinstance(value, str) else f"[{', '.join(value)}]"


def layer_of(name: str) -> str | None:
    """Decide a table's layer from its name alone; nothing is looked up.

    Parameters
    ----------
    name
        A dotted table name as written.

    Returns
    -------
    str or None
        The first part when the name has more than one part and that part is one of `LAYERS`,
        compared exactly (``Gold.x`` is in no layer); `None` for a source and for an
        unqualified name.
    """
    first, dot, _ = name.partition(".")
    return first if dot and first in LAYERS else None


class LayerRules(BaseModel):
    """The rules of one layer.

    Parameters
    ----------
    reads
        The layers its assets may read, in the order written; empty: sources only for bronze,
        nothing for the others.
    write
        The write modes its assets may use, from `LAYER_WRITES`; empty: the project builds no
        asset in the layer.
    kinds
        `KINDS_NONE`, or the model kinds one of which every asset must declare.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reads: tuple[str, ...]
    write: tuple[str, ...]
    kinds: str | tuple[str, ...]

    def as_flow(self) -> str:
        """Render the rules as one line of YAML flow style.

        Returns
        -------
        str
            ``{reads: [...], write: [...], kinds: ...}``.
        """
        return (
            f"{{reads: {flow_value(self.reads)}, write: {flow_value(self.write)}, "
            f"kinds: {flow_value(self.kinds)}}}"
        )


class LayersConfig(BaseModel):
    """`lakana.yaml`'s `layers:` section: the rules of each of the three layers.

    Parameters
    ----------
    bronze
        The rules of bronze.
    silver
        The rules of silver.
    gold
        The rules of gold.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bronze: LayerRules
    silver: LayerRules
    gold: LayerRules

    @model_validator(mode="after")
    def _rules_are_valid(self) -> LayersConfig:
        """Refuse a value `lakana.yaml` would refuse, so a stored copy is checked the same way.

        Returns
        -------
        LayersConfig
            `self`, unchanged.

        Raises
        ------
        ValueError
            With the first text of `rule_problems`.
        """
        for layer in LAYERS:
            rules = self.for_layer(layer)
            for rule in RULES:
                problems = rule_problems(layer, rule, getattr(rules, rule))
                if problems:
                    raise ValueError(problems[0])
        return self

    def for_layer(self, layer: str) -> LayerRules:
        """Return the rules of one layer.

        Parameters
        ----------
        layer
            One of `LAYERS`.

        Returns
        -------
        LayerRules
            Its rules.
        """
        rules: LayerRules = getattr(self, layer)
        return rules


def rule_problems(layer: str, rule: str, value: object) -> list[str]:
    """Check the value of one rule.

    Parameters
    ----------
    layer
        The layer, for the texts.
    rule
        One of `RULES`.
    value
        The value as the document or a stored manifest gave it.

    Returns
    -------
    list of str
        The spec's texts, each prefixed ``layers.<layer>.<rule>:``: a value that is not a list
        (or not `none` for `kinds`), `any`, an empty `kinds` list, then one per value that is
        not allowed and one per value listed twice, each said once; empty when the value is fine.
    """
    where = f"layers.{layer}.{rule}"
    if rule == "kinds":
        if value == KINDS_NONE:
            return []
        if value == "any":
            return [f"{where}: there is no `any`; {_LIST_KINDS}"]
        if not isinstance(value, list | tuple):
            return [f"{where}: must be `none` or a list of kinds, such as {_KINDS_EXAMPLE}"]
        if not value:
            return [f"{where}: an empty list would allow no asset; {_LIST_KINDS}"]
        allowed: Sequence[str] = MODEL_KINDS
        unknown = f"is not a kind; list {or_list(MODEL_KINDS)}, or write `none`"
    elif not isinstance(value, list | tuple):
        example = flow_value(getattr(MEDALLION_LAYERS.for_layer(layer), rule))
        return [f"{where}: must be a list, such as `{example}`"]
    elif rule == "reads":
        allowed = LAYERS
        unknown = (
            f"is not a layer; list {or_list(LAYERS)}. Tables outside them are sources, which "
            f"only {SOURCE_LAYER} reads"
        )
    else:
        allowed = LAYER_WRITES
        unknown = (
            f"is not a write mode a layer takes; list {or_list(LAYER_WRITES)} "
            "(`materialize: incremental` counts as append)"
        )
    problems: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            message = f"{where}: `{shown_key(item)}` {unknown}"
        elif item in seen:
            message = f"{where}: lists `{item}` twice"
        else:
            seen.add(item)
            continue
        if message not in problems:
            problems.append(message)
    return problems


def _section_issues(section: object, *, text: str, path: str) -> list[ProjectIssue]:
    """Check the raw `layers:` value: its shape, its layers, its rules and their values.

    Parameters
    ----------
    section
        The value of `layers:` as the YAML gave it.
    text
        The file's normalized content, for the lines.
    path
        The file's repository-relative path.

    Returns
    -------
    list of ProjectIssue
        An empty or non-mapping section alone; otherwise every unknown layer in document order,
        then per layer of `LAYERS`: a missing or non-mapping layer, or per rule of `RULES` its
        absence or the problems of its value, then its unknown rules.
    """
    top = key_line(text, ("layers",))
    if section is None or section == {}:
        return [
            ProjectIssue(
                path,
                top,
                f"layers: is empty; declare {and_list(LAYERS)}, each with reads, write and kinds, "
                "or remove `layers:` to deploy without layer rules. The medallion default: "
                f"`{MEDALLION_LAYERS_FLOW}`",
            )
        ]
    if not isinstance(section, dict):
        bronze = MEDALLION_LAYERS.for_layer(SOURCE_LAYER).as_flow()
        return [
            ProjectIssue(
                path,
                top,
                f"layers: must map {and_list(LAYERS)} to their rules, such as "
                f"`{SOURCE_LAYER}: {bronze}`",
            )
        ]
    issues = [
        ProjectIssue(
            path,
            key_line(text, ("layers", key)),
            f"layers: `{shown_key(key)}` is not a layer; Lakana's layers are {and_list(LAYERS)}, "
            "the top-level namespaces every table lives under. Remove it, or put its rules under "
            "one of those",
        )
        for key in section
        if key not in LAYERS
    ]
    for layer in LAYERS:
        default = MEDALLION_LAYERS.for_layer(layer)
        if layer not in section:
            issues.append(
                ProjectIssue(
                    path,
                    top,
                    f"layers: needs {layer}; once lakana.yaml has `layers:`, it declares "
                    f"{and_list(LAYERS)}, each with reads, write and kinds. The medallion default "
                    f"for {layer}: `{layer}: {default.as_flow()}`",
                )
            )
            continue
        rules = section[layer]
        line = key_line(text, ("layers", layer))
        if not isinstance(rules, dict):
            issues.append(
                ProjectIssue(
                    path,
                    line,
                    f"layers.{layer}: must be a mapping of reads, write and kinds, such as "
                    f"`{default.as_flow()}`",
                )
            )
            continue
        for rule in RULES:
            if rule not in rules:
                issues.append(
                    ProjectIssue(
                        path,
                        line,
                        f"layers.{layer}: needs `{rule}:`; every layer declares reads, write and "
                        f"kinds. The medallion default: `{rule}: "
                        f"{flow_value(getattr(default, rule))}`",
                    )
                )
                continue
            rule_line = key_line(text, ("layers", layer, rule))
            issues.extend(
                ProjectIssue(path, rule_line, message)
                for message in rule_problems(layer, rule, rules[rule])
            )
        issues.extend(
            ProjectIssue(
                path,
                key_line(text, ("layers", layer, key)),
                f"layers.{layer}: `{shown_key(key)}` is not a rule; a layer declares reads, "
                "write and kinds",
            )
            for key in rules
            if key not in RULES
        )
    return issues


def layer_config_issues(raw: dict[Any, Any], *, text: str, path: str) -> list[ProjectIssue]:
    """Check `layers:` before pydantic, and take a refused section out of the document.

    Pydantic would word a missing layer or rule as ``Field required`` and could not show the
    default, so the raw document is checked first, as `_refuse_keep_as_default` does.

    Parameters
    ----------
    raw
        The parsed document. A `layers` value with any issue is removed from it in place, so
        pydantic does not report it a second time.
    text
        The file's normalized content, for the lines.
    path
        The file's repository-relative path.

    Returns
    -------
    list of ProjectIssue
        `_section_issues`' issues; empty when the document has no `layers:` or a valid one.
    """
    if "layers" not in raw:
        return []
    issues = _section_issues(raw["layers"], text=text, path=path)
    if issues:
        del raw["layers"]
    return issues


MEDALLION_LAYERS = LayersConfig(
    bronze=LayerRules(reads=(), write=("append", "merge"), kinds=KINDS_NONE),
    silver=LayerRules(
        reads=("bronze", "silver"), write=("replace", "append", "merge", "view"), kinds=("staging",)
    ),
    gold=LayerRules(
        reads=("silver", "gold"),
        write=("replace", "append", "merge", "view"),
        kinds=("fact", "dimension", "wide"),
    ),
)
"""The medallion default: what `--template medallion` (#254) writes and every message shows.
Spelled out, not built from `LAYER_WRITES`, so a new write mode joins it only on purpose."""

MEDALLION_LAYERS_FLOW = (
    "layers: {"
    + ", ".join(f"{layer}: {MEDALLION_LAYERS.for_layer(layer).as_flow()}" for layer in LAYERS)
    + "}"
)
"""`MEDALLION_LAYERS` as one line of YAML, ready to paste: every issue is one line."""
