"""SQL front-matter: a `/* lakana … */` block, the first thing in an asset file."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import pydantic
from pydantic import Field, StrictBool, field_validator

from lakana_core.artifacts.manifest import shown
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef, validate_path_component
from lakana_core.projects.asset_model import (
    CHECK_FAMILIES,
    CHECK_MODES,
    KEY_COLUMN_MESSAGE,
    KEY_NONE,
    MAX_GRAIN_CHARS,
    MODEL_KINDS,
    WATERMARK_MESSAGE,
    WRITE_CHOICES,
    key_issues,
    write_choice_message,
    write_rule_issues,
)
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.measures import (
    MAX_MEASURE_COLUMN_BYTES,
    MAX_MEASURES,
    MEASURE_PREFIX,
    MeasureSpec,
)
from lakana_core.projects.project_config import (
    FREE_TEXT_MESSAGE,
    IDENTIFIER_MESSAGE,
    MAX_RETRIES,
    AssetDefaults,
    Duration,
    Materialize,
    PositiveDuration,
    SafeTextModel,
    normalize_source,
    pydantic_issues,
    unsafe_free_text,
    unsafe_identifier,
)
from lakana_core.projects.yaml_loader import YamlError, load_yaml

_OPENING = re.compile(r"\A(\s*)/\*[ \t]*lakana[ \t]*\n")
"""A lakana block at the very start, after optional whitespace."""

_ANYWHERE = re.compile(r"/\*[ \t]*lakana[ \t]*\n")
"""A lakana block opening anywhere — used to report one that is not first."""

_PYTHON_ONLY_KEYS = {
    "output": '@asset(output="artifact")',
    "keep_versions": '@asset(output="artifact", keep_versions=N)',
    "decimals": '@asset(decimals="exact")',
    "connections": '@asset(connections=["crm-db"])',
}
"""Decorator arguments of a Python asset that a SQL block lacks, and how to declare them."""

_PYTHON_ONLY = (
    "`{key}` applies only to a Python asset, not to a SQL file; declare the asset in a "
    "`.py` file with `{declaration}`, or remove `{key}` from this block"
)


class AssetMetadata(SafeTextModel):
    """What one asset's front-matter may say.

    Parameters
    ----------
    description
        Human description.
    owner
        Owning team or person.
    tags
        Free-form tags.
    materialize
        ``table``, ``view`` or ``incremental``.
    incremental_key
        The watermark column; required exactly when ``incremental``.
    evolve_schema
        Let an incremental append evolve the table's schema.
    write
        ``replace``, ``append`` or ``merge``; `None` when the asset does not say (it then
        replaces, or appends for ``materialize: incremental``).
    kind
        ``fact``, ``dimension``, ``wide`` or ``staging``; `None` when the asset does not say.
    grain
        One line saying what one row of the table is.
    key
        The columns that identify a row (needed for ``write: merge``), or ``none`` to say that
        no columns identify one.
    references
        Column → the table (``ns.table``, or ``ns.table.column``) its values must exist in.
    measures
        Column → its measure (aggregation, additivity, unit or currency, label, description).
    checks
        Check family (``key``, ``references``, ``measures``) → what a failure does (``block``,
        ``keep`` or ``warn``).
    watermark
        The column whose largest value the next run starts after, for ``write: append`` (and
        optionally ``merge``).
    columns
        Column name → description.
    retries
        Retries after a failed attempt, for this asset.
    retry_delay
        First retry delay for this asset; doubled per attempt.
    timeout
        Wall-clock limit of one attempt, for this asset.
    """

    description: str | None = None
    owner: str | None = None
    tags: tuple[str, ...] = ()
    materialize: Materialize | None = None
    incremental_key: str | None = None
    evolve_schema: StrictBool | None = None
    write: Literal["replace", "append", "merge"] | None = None
    kind: Literal["fact", "dimension", "wide", "staging"] | None = None
    grain: str | None = None
    key: tuple[str, ...] | Literal["none"] = ()
    references: dict[str, str] = Field(default_factory=dict)
    measures: dict[str, MeasureSpec] = Field(default_factory=dict)
    checks: dict[str, str] = Field(default_factory=dict)
    watermark: str | None = None
    columns: dict[str, str] = Field(default_factory=dict)
    retries: int | None = Field(default=None, ge=0, le=MAX_RETRIES, strict=True)
    retry_delay: Duration | None = None
    timeout: PositiveDuration | None = None

    @field_validator("incremental_key")
    @classmethod
    def _incremental_key_is_safe(cls, value: str | None) -> str | None:
        """Refuse an incremental key with an unsafe character.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        ValueError
            If it has an unsafe character.
        """
        if value is not None and unsafe_identifier(value):
            raise ValueError(f"incremental_key {IDENTIFIER_MESSAGE}")
        return value

    @field_validator("write", mode="before")
    @classmethod
    def _write_is_a_choice(cls, value: object) -> object:
        """Refuse a `write` that is not one of `WRITE_CHOICES`, naming them.

        Parameters
        ----------
        value
            The field's raw value.

        Returns
        -------
        object
            `value`, for the type check that follows.

        Raises
        ------
        ValueError
            If it is a string that is not a choice; any other type is left to the type check.
        """
        if isinstance(value, str) and value not in WRITE_CHOICES:
            raise ValueError(write_choice_message(shown(value)).removeprefix("write "))
        return value

    @field_validator("kind", mode="before")
    @classmethod
    def _kind_is_a_choice(cls, value: object) -> object:
        """Refuse a `kind` that is not one of `MODEL_KINDS`, naming them.

        Parameters
        ----------
        value
            The field's raw value.

        Returns
        -------
        object
            `value`, for the type check that follows.

        Raises
        ------
        ValueError
            If it is a string that is not a kind.
        """
        if isinstance(value, str) and value not in MODEL_KINDS:
            raise ValueError(f"must be one of {', '.join(MODEL_KINDS)}; got '{shown(value)}'")
        return value

    @field_validator("key", mode="before")
    @classmethod
    def _key_is_one_spelling(cls, value: object) -> object:
        """Read a single column name as a one-column key; `none` stays the escape.

        Parameters
        ----------
        value
            The field's raw value.

        Returns
        -------
        object
            `value`, or a one-tuple for a string other than `none`.
        """
        if isinstance(value, str) and value != KEY_NONE:
            return (value,)
        return value

    @field_validator("key")
    @classmethod
    def _key_is_safe(cls, value: tuple[str, ...] | str) -> tuple[str, ...] | str:
        """Refuse a key column with an unsafe character.

        A column listed twice is a consistency issue (`consistency_issues`), reported with the
        other rules.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        tuple of str or str
            `value`.

        Raises
        ------
        ValueError
            If a column has an unsafe character.
        """
        if not isinstance(value, str) and any(unsafe_identifier(column) for column in value):
            raise ValueError(KEY_COLUMN_MESSAGE)
        return value

    @field_validator("grain")
    @classmethod
    def _grain_is_one_line(cls, value: str | None) -> str | None:
        """Refuse a grain that is empty, long, several lines or has an unsafe character.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value` without surrounding whitespace.

        Raises
        ------
        ValueError
            If it breaks one of those rules.
        """
        if value is None:
            return value
        text = value.strip()
        if not text:
            raise ValueError("must not be empty: say in one sentence what one row is")
        if "\n" in text:
            raise ValueError("must be one line")
        if len(text) > MAX_GRAIN_CHARS:
            raise ValueError(f"must be at most {MAX_GRAIN_CHARS} characters")
        if unsafe_free_text(value):
            raise ValueError(FREE_TEXT_MESSAGE)
        return text

    @field_validator("references")
    @classmethod
    def _references_are_tables(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse a reference column or target that cannot be used.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        dict
            `value`.

        Raises
        ------
        ValueError
            If a column has an unsafe character or cannot name its check, or a target is not
            a table name.
        """
        for column, target in value.items():
            if not column:
                raise ValueError("a reference column must not be empty")
            if unsafe_identifier(column):
                raise ValueError(f"a reference column {IDENTIFIER_MESSAGE}")
            if unsafe_identifier(target):
                # Before any message shows the target: it could forge a line of the issues.
                raise ValueError(f"reference `{column}` names a target that {IDENTIFIER_MESSAGE}")
            try:
                validate_path_component(f"ref_{column}", what="Check name")
            except ValidationError as exc:
                raise ValueError(
                    f"reference column `{column}` cannot name its check `ref_{column}`: {exc}"
                ) from exc
            try:
                TableRef.from_dotted("-", target)
            except ValidationError as exc:
                raise ValueError(
                    f"reference `{column}: {shown(target)}`: {exc} "
                    "Write `ns.table` or `ns.table.column`."
                ) from exc
        return value

    @field_validator("measures", mode="before")
    @classmethod
    def _measures_are_mappings(cls, value: object) -> object:
        """Refuse a measure that is not a mapping, before pydantic's own text for it.

        Parameters
        ----------
        value
            The field's raw value.

        Returns
        -------
        object
            `value`, for the type check that follows.

        Raises
        ------
        ValueError
            For the first measure whose value is not a mapping.
        """
        if isinstance(value, Mapping):
            for column, spec in value.items():
                if isinstance(column, str) and not isinstance(spec, Mapping):
                    raise ValueError(
                        f"`{shown(column)}` must be a mapping such as "
                        "`{aggregation: sum, currency: EUR}`"
                    )
        return value

    @field_validator("measures")
    @classmethod
    def _measure_columns_are_usable(cls, value: dict[str, MeasureSpec]) -> dict[str, MeasureSpec]:
        """Refuse too many measures, or a measure column that cannot be stored or named.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        dict
            `value`.

        Raises
        ------
        ValueError
            For more than `MAX_MEASURES` measures, then for the first column that is empty,
            unsafe, longer than `MAX_MEASURE_COLUMN_BYTES`, cannot name its id, or is in its own
            `not_over`.
        """
        if len(value) > MAX_MEASURES:
            raise ValueError(
                f"at most {MAX_MEASURES} measures per table, and `measures:` lists {len(value)}; "
                "keep the ones people aggregate, and describe the others in `columns:`"
            )
        for column, spec in value.items():
            if not column:
                raise ValueError("a measure column must not be empty")
            if unsafe_identifier(column):
                raise ValueError(f"a measure column {IDENTIFIER_MESSAGE}")
            size = len(column.encode("utf-8"))
            if size > MAX_MEASURE_COLUMN_BYTES:
                raise ValueError(
                    f"measure column `{column}` has {size} bytes; a measure column may have at "
                    f"most {MAX_MEASURE_COLUMN_BYTES} bytes"
                )
            try:
                validate_path_component(f"{MEASURE_PREFIX}{column}", what="Measure id")
            except ValidationError as exc:
                raise ValueError(
                    f"measure column `{column}` cannot name its id `{MEASURE_PREFIX}{column}`: "
                    f"{exc}"
                ) from exc
            if column in spec.not_over:
                raise ValueError(
                    f"measure `{column}` names itself in `not_over`; list the other columns it "
                    "must not be summed over, such as a date"
                )
        return value

    @field_validator("checks")
    @classmethod
    def _checks_are_known(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse a check family or failure mode that does not exist.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        dict
            `value`.

        Raises
        ------
        ValueError
            If a family is not in `CHECK_FAMILIES` or a mode not in `CHECK_MODES`.
        """
        for family, mode in value.items():
            if family not in CHECK_FAMILIES:
                raise ValueError(
                    f"takes only {', '.join(CHECK_FAMILIES[:-1])} and {CHECK_FAMILIES[-1]}"
                )
            if mode not in CHECK_MODES:
                raise ValueError(f"{family} must be block, keep or warn")
        return value

    @field_validator("watermark")
    @classmethod
    def _watermark_is_safe(cls, value: str | None) -> str | None:
        """Refuse a watermark column with an unsafe character.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        ValueError
            If it has an unsafe character.
        """
        if value is not None and unsafe_identifier(value):
            raise ValueError(WATERMARK_MESSAGE)
        return value

    @field_validator("columns")
    @classmethod
    def _columns_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse a column name or description with an unsafe character.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        dict
            `value`.

        Raises
        ------
        ValueError
            If it has an unsafe character.
        """
        if any(unsafe_identifier(name) for name in value):
            raise ValueError(f"a column name {IDENTIFIER_MESSAGE}")
        if any(unsafe_free_text(text) for text in value.values()):
            raise ValueError(f"a column description {FREE_TEXT_MESSAGE}")
        return value


@dataclass(frozen=True)
class FrontMatter:
    """An asset file split into its metadata and its SQL body.

    Parameters
    ----------
    metadata
        The block's metadata, or an empty `AssetMetadata` with no block.
    body
        Everything after the block (the whole file with no block).
    body_line
        1-based file line on which `body` starts.
    block_line
        1-based file line of ``/* lakana``, or `None` with no block.
    cut_hint
        Set when text after the block hints that a ``*/`` inside the YAML ended
        it early. Only a hint: the body may be valid SQL (``'*/'`` in a string),
        so it is shown only if the body then fails to parse.
    """

    metadata: AssetMetadata
    body: str
    body_line: int
    block_line: int | None
    cut_hint: str | None = None


def _cut_hint(text: str, close: int) -> str | None:
    """Suggest that a stray ``*/`` inside the YAML ended the block early.

    The block closes at the first ``*/`` whatever YAML says, so a ``*/`` in a
    string or scalar leaves the rest of the block in the SQL body. A ``*/``
    before the body's first ``/*`` is only a hint of that: valid SQL can hold
    one too (``'*/'`` in a string literal, a ``--`` comment). Callers use it
    to explain a failure, never to cause one.

    Parameters
    ----------
    text
        The normalized file.
    close
        Offset of the ``*/`` that closed the block.

    Returns
    -------
    str or None
        The message, or `None` when no ``*/`` precedes the body's first ``/*``.
    """
    rest = text[close + 2 :]
    stray = rest.find("*/")
    opens = rest.find("/*")
    if stray == -1 or (opens != -1 and opens < stray):
        return None
    line = text.count("\n", 0, close) + 1
    return (
        f"the block ends at the first `*/`, on line {line}, even inside a string or scalar; "
        "another `*/` follows, so the YAML may have been cut short"
    )


@dataclass(frozen=True)
class RawFrontMatter:
    """A file split into its block's mapping and its SQL body, before any key is checked.

    Parameters
    ----------
    raw
        The block's mapping; empty with no block or an empty one.
    body
        Everything after the block (the whole file with no block).
    body_line
        1-based file line on which `body` starts.
    block_line
        1-based file line of ``/* lakana``, or `None` with no block.
    cut_hint
        As `FrontMatter.cut_hint`.
    """

    raw: dict[str, object]
    body: str
    body_line: int
    block_line: int | None
    cut_hint: str | None


def split_front_matter(
    text: str, *, path: str, limits: DeployLimits | None = None
) -> RawFrontMatter:
    """Split a file into its `/* lakana */` block's mapping and its body.

    Parameters
    ----------
    text
        The file's decoded content.
    path
        Repository-relative path, for issues.
    limits
        Supplies the per-block YAML size limit; the defaults when `None`.

    Returns
    -------
    RawFrontMatter
        The mapping, unvalidated, and the body.

    Raises
    ------
    ProjectValidationError
        If the block is not first, is unterminated, too large, not a
        mapping, or uses an anchor, alias, merge key or duplicate key.
    """
    limits = limits or DeployLimits()
    text = normalize_source(text)
    opening = _OPENING.match(text)
    if opening is None:
        stray = _ANYWHERE.search(text)
        if stray is not None:
            line = text.count("\n", 0, stray.start()) + 1
            raise ProjectValidationError(
                [
                    ProjectIssue(
                        path, line, "the /* lakana */ block must be the first thing in the file"
                    )
                ]
            )
        return RawFrontMatter(raw={}, body=text, body_line=1, block_line=None, cut_hint=None)
    block_line = text.count("\n", 0, opening.end(1)) + 1
    close = text.find("*/", opening.end())
    if close == -1:
        raise ProjectValidationError(
            [ProjectIssue(path, block_line, "the /* lakana block is never closed")]
        )
    block = text[opening.end() : close]
    cut_hint = _cut_hint(text, close)
    try:
        raw = load_yaml(block, what=f"{path} front-matter", max_bytes=limits.yaml_bytes)
    except YamlError as exc:
        line = block_line + exc.line if exc.line is not None else block_line
        message = str(exc) + (f" ({cut_hint})" if cut_hint is not None else "")
        raise ProjectValidationError([ProjectIssue(path, line, message)]) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProjectValidationError(
            [ProjectIssue(path, block_line, "front-matter must be a mapping")]
        )
    body_start = close + 2
    return RawFrontMatter(
        raw=raw,
        body=text[body_start:],
        body_line=text.count("\n", 0, body_start) + 1,
        block_line=block_line,
        cut_hint=cut_hint,
    )


def parse_front_matter(text: str, *, path: str, limits: DeployLimits | None = None) -> FrontMatter:
    """Split and validate an asset file's front-matter.

    Parameters
    ----------
    text
        The file's decoded content.
    path
        Repository-relative path, for issues.
    limits
        Supplies the per-block YAML size limit; the defaults when `None`.

    Returns
    -------
    FrontMatter
        The metadata and the body.

    Raises
    ------
    ProjectValidationError
        If the block is not first, is unterminated, too large, not a
        mapping, uses an anchor, alias, merge key or duplicate key, or has an unknown key.
        `output` and `keep_versions` are refused with the way to declare a Python asset;
        their values are never read or printed.
        A cut-short block is not an error here; see `FrontMatter.cut_hint`.
    """
    split = split_front_matter(text, path=path, limits=limits)
    if split.block_line is None:
        return FrontMatter(metadata=AssetMetadata(), body=split.body, body_line=1, block_line=None)
    block_line = split.block_line
    raw = split.raw
    python_only = [
        ProjectIssue(path, block_line, _PYTHON_ONLY.format(key=key, declaration=declaration))
        for key, declaration in _PYTHON_ONLY_KEYS.items()
        if key in raw
    ]
    raw = {key: value for key, value in raw.items() if key not in _PYTHON_ONLY_KEYS}
    try:
        metadata = AssetMetadata.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise ProjectValidationError(
            [*python_only, *pydantic_issues(exc, path=path, line=block_line)]
        ) from exc
    if python_only:
        raise ProjectValidationError(python_only)
    return FrontMatter(
        metadata=metadata,
        body=split.body,
        body_line=split.body_line,
        block_line=block_line,
        cut_hint=split.cut_hint,
    )


def merge_metadata(defaults: AssetDefaults, own: AssetMetadata) -> AssetMetadata:
    """Apply `lakana.yaml` defaults under an asset's own front-matter.

    Parameters
    ----------
    defaults
        The resolved `defaults` section.
    own
        The asset's front-matter.

    Returns
    -------
    AssetMetadata
        Own values where set, defaults otherwise; `materialize` is never
        `None` (``table`` when neither sets it).
    """
    return own.model_copy(
        update={
            "description": own.description if own.description is not None else defaults.description,
            "owner": own.owner if own.owner is not None else defaults.owner,
            "tags": own.tags or defaults.tags or (),
            "materialize": own.materialize or defaults.materialize or "table",
            "retries": own.retries if own.retries is not None else defaults.retries,
            "retry_delay": (
                own.retry_delay if own.retry_delay is not None else defaults.retry_delay
            ),
            "timeout": own.timeout if own.timeout is not None else defaults.timeout,
        }
    )


def consistency_issues(
    metadata: AssetMetadata, *, path: str, line: int | None
) -> list[ProjectIssue]:
    """Check the materialization options against each other.

    Parameters
    ----------
    metadata
        Merged metadata (`merge_metadata`).
    path
        Repository-relative path, for issues.
    line
        The block's line, if any.

    Returns
    -------
    list of ProjectIssue
        Empty when consistent.
    """
    issues: list[ProjectIssue] = []
    incremental = metadata.materialize == "incremental"
    if incremental and metadata.incremental_key is None:
        issues.append(
            ProjectIssue(path, line, "incremental_key is required for materialize: incremental")
        )
    if not incremental and metadata.incremental_key is not None:
        issues.append(
            ProjectIssue(
                path, line, "incremental_key is allowed only with materialize: incremental"
            )
        )
    rules = [
        *write_rule_issues(
            metadata.write,
            metadata.key,
            metadata.watermark,
            materialize=metadata.materialize,
            evolve_schema=metadata.evolve_schema,
        ),
        *key_issues(metadata.key),
    ]
    issues.extend(ProjectIssue(path, line, message) for message in rules)
    return issues
