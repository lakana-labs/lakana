"""Glossary files: `glossary/<area>.yaml`, each a map of term id → term (#375).

A term has a name, a definition, synonyms, an owner, links to tables and columns, and related
terms. This module finds the files, parses them with the YAML rules of every project file and
applies the form rules. The deploy checks links, related ids and ownership against the environment
(`DeployService.validate_glossary`) and stores the result (`lakana_core.catalog.glossary`).
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import pydantic
from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.projects.discovery import (
    GLOSSARY_DIR,
    _decode,
    _read,
    folder_entry_refused,
)
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.project_config import (
    IDENTIFIER_MESSAGE,
    MAX_DESCRIPTION_CHARS,
    SafeTextModel,
    StrictModel,
    checked_label,
    checked_text,
    pydantic_issues,
    unsafe_identifier,
)
from lakana_core.projects.yaml_loader import (
    KeyLines,
    YamlError,
    YamlTooManyEntries,
    load_yaml_with_lines,
    shown_key,
)

MAX_GLOSSARY_FILES = 50
"""The most YAML files `glossary/` may hold."""

MAX_GLOSSARY_FILE_BYTES = 1024 * 1024
"""The largest glossary file."""

MAX_TERMS = 2000
"""The most terms one repository may define."""

MAX_SYNONYMS = 20
"""The most synonyms of a term per language."""

MAX_LINKED_TABLES = 50
"""The most tables one term links."""

MAX_LINKED_COLUMNS = 200
"""The most columns one term links, over all its tables."""

MAX_RELATED = 20
"""The most term ids one `related:` names."""

MAX_GLOSSARY_ENTRIES = 50_000
"""The most keys and list items one file may hold, counted while it is parsed."""

TERM_ID = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")
"""The form of a term id: the YAML key."""

TERM_FIELDS = ("name", "definition", "owner", "synonyms", "links", "related")
"""The fields a term may have, in the order the messages name them."""

_FIELD_LIST = "`name`, `definition`, `owner`, `synonyms`, `links` and `related`"
_REQUIRED = {
    "name": "the term as people say it, such as `name: Net revenue`",
    "definition": "what the term means, in a sentence or two",
    "owner": "the team or person who answers for the term, such as `owner: finance`",
}
TERM_ID_RULE = (
    "a term id is lowercase letters, digits and underscores, starts with a letter and has at "
    "most 63 characters, such as `net_revenue`"
)
_LINK = "symlinks are not allowed under glossary/"
_NO_FOLDERS = (
    "glossary/ holds YAML files, not folders; put the terms in files directly under glossary/, "
    "such as glossary/sales.yaml"
)
_NOT_A_FOLDER = (
    "glossary must be a folder of YAML files of terms, such as glossary/sales.yaml; rename this "
    "file, or move it out of the repository's root"
)


@dataclass(frozen=True)
class DiscoveredGlossaryFile:
    """One glossary file as discovery read it.

    Parameters
    ----------
    path
        Repository-relative path.
    text
        The decoded content.
    """

    path: str
    text: str


def discover_glossary(
    root: Path,
) -> tuple[tuple[DiscoveredGlossaryFile, ...] | None, list[ProjectIssue]]:
    """Find every YAML file directly under ``glossary/``.

    Parameters
    ----------
    root
        The extracted checkout.

    Returns
    -------
    tuple
        The files sorted by name (`None` when the repository has no `glossary`), and every
        problem: a `glossary` that is a link or not a folder, links, folders, a `.yml` or
        other-case suffix, oversized or non-UTF-8 files, and more than `MAX_GLOSSARY_FILES`
        YAML files (those past it are not read). Files of other kinds are ignored.
    """
    folder = root / GLOSSARY_DIR
    if folder.is_symlink():
        return None, [ProjectIssue(GLOSSARY_DIR, None, _LINK)]
    if not os.path.lexists(folder):
        return None, []
    if not folder.is_dir():
        return None, [ProjectIssue(GLOSSARY_DIR, None, _NOT_A_FOLDER)]
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except OSError as exc:
        return None, [ProjectIssue(GLOSSARY_DIR, None, f"cannot be read: {exc.strerror}")]
    found: list[DiscoveredGlossaryFile] = []
    issues: list[ProjectIssue] = []
    files = 0
    for entry in entries:
        relative = f"{GLOSSARY_DIR}/{entry.name}"
        if folder_entry_refused(entry, relative, _LINK, _NO_FOLDERS, issues):
            continue
        stem, dot, suffix = entry.name.rpartition(".")
        if not dot or suffix.lower() not in ("yaml", "yml"):
            continue  # a README or notes: not a glossary file
        if suffix != "yaml":
            issues.append(
                ProjectIssue(
                    relative,
                    None,
                    f"a glossary file ends in .yaml; rename it to glossary/{stem}.yaml",
                )
            )
            continue
        files += 1
        if files > MAX_GLOSSARY_FILES:
            continue
        data = _read(Path(entry.path), relative, MAX_GLOSSARY_FILE_BYTES, issues)
        text = _decode(data, relative, issues) if data is not None else None
        if text is not None:
            found.append(DiscoveredGlossaryFile(relative, text))
    if files > MAX_GLOSSARY_FILES:
        issues.append(
            ProjectIssue(
                GLOSSARY_DIR,
                None,
                f"glossary/ holds {files} YAML files, and a repository has at most "
                f"{MAX_GLOSSARY_FILES}; merge some of them, such as one file per area",
            )
        )
    return tuple(found), issues


def _one_line(value: str) -> str:
    """Apply a one-line field's rules: a label's rules, and no tab.

    Parameters
    ----------
    value
        The text as written.

    Returns
    -------
    str
        The stripped text.

    Raises
    ------
    ValueError
        For the first rule it breaks.
    """
    text = checked_label(value)
    if "\t" in text:
        raise ValueError("must not contain a tab; write it on one line with spaces")
    return text


def _definition(value: str) -> str:
    """Apply a definition's rules: not empty, at most 4,000 characters, safe.

    Parameters
    ----------
    value
        The text as written.

    Returns
    -------
    str
        The stripped text.
    """
    return checked_text(value, limit=MAX_DESCRIPTION_CHARS)


OneLine = Annotated[str, AfterValidator(_one_line)]
"""A name or a synonym."""

Definition = Annotated[str, AfterValidator(_definition)]
"""A definition."""


def term_id_problem(key: object) -> str | None:
    """Say why a key is not a term id.

    Parameters
    ----------
    key
        The key as YAML read it.

    Returns
    -------
    str or None
        ``is not a term id; …``, or `None` for a valid id.
    """
    if isinstance(key, str) and TERM_ID.fullmatch(key):
        return None
    return f"is not a term id; {TERM_ID_RULE}"


def synonym_problem(synonyms: Sequence[str], name: str | None) -> str | None:
    """Apply the synonym rules of one language.

    Parameters
    ----------
    synonyms
        The synonyms, validated one by one.
    name
        The term's name in the same language, or `None` when that language has none.

    Returns
    -------
    str or None
        The first rule broken, or `None`.
    """
    if len(synonyms) > MAX_SYNONYMS:
        return (
            f"has {len(synonyms)} synonyms, and a term has at most {MAX_SYNONYMS} per language; "
            "keep the words people search for"
        )
    seen: set[str] = set()
    for synonym in synonyms:
        folded = synonym.casefold()
        if folded in seen:
            return f"lists `{synonym}` twice (case does not count); remove one"
        seen.add(folded)
        if name is not None and folded == name.casefold():
            return (
                f"`{synonym}` is the term's name; a synonym is another word for it, so remove it "
                "here"
            )
    return None


def _check_synonyms(value: tuple[str, ...], name: str | None) -> tuple[str, ...]:
    """Raise the first synonym rule `value` breaks.

    Parameters
    ----------
    value
        The synonyms.
    name
        The term's name in the same language, or `None`.

    Returns
    -------
    tuple of str
        `value`.

    Raises
    ------
    ValueError
        For the first rule broken.
    """
    problem = synonym_problem(value, name)
    if problem is not None:
        raise ValueError(problem)
    return value


def _is_table_name(table: object) -> bool:
    """Say whether a `links:` key is a dotted table name.

    Parameters
    ----------
    table
        The key as YAML read it.

    Returns
    -------
    bool
        Whether `TableRef.from_dotted` accepts it.
    """
    if not isinstance(table, str) or "." not in table:
        return False
    try:
        TableRef.from_dotted("-", table)
    except ValidationError:
        return False
    return True


def _links_shape(value: object) -> object:
    """Refuse a `links:` value that is not table → list of column names, before pydantic does.

    Parameters
    ----------
    value
        The raw `links:` value.

    Returns
    -------
    object
        `value`, for the type check that follows.

    Raises
    ------
    ValueError
        For the first problem.
    """
    if not isinstance(value, Mapping):
        raise ValueError(
            "must be a mapping of tables to their columns, such as "
            "`{gold.fact_sales: [net_amount], gold.dim_store: []}`"
        )
    if len(value) > MAX_LINKED_TABLES:
        raise ValueError(
            f"links {len(value)} tables, and a term links at most {MAX_LINKED_TABLES}; link the "
            "tables people should use for the term"
        )
    columns = 0
    for table, listed in value.items():
        if not _is_table_name(table):
            raise ValueError(
                f"`{shown_key(table)}` is not a table name; write the table's dotted name, as "
                "the project builds it, such as `gold.fact_sales`"
            )
        if not isinstance(listed, list) or not all(isinstance(c, str) for c in listed):
            raise ValueError(
                f"`{table}` must be a list of its columns, such as `[net_amount]`; write `[]` "
                "to link the table itself"
            )
        columns += len(listed)
        if columns > MAX_LINKED_COLUMNS:
            raise ValueError(
                f"links {columns} columns, and a term links at most {MAX_LINKED_COLUMNS}; link "
                "the columns people should use for the term"
            )
        seen: set[str] = set()
        for column in listed:
            if not column.strip() or unsafe_identifier(column):
                raise ValueError(
                    f"a column of `{table}` {IDENTIFIER_MESSAGE} and must not be empty"
                )
            if column in seen:
                raise ValueError(f"lists column `{column}` of `{table}` twice; remove one")
            seen.add(column)
    return value


class TermSpec(SafeTextModel):
    """One term as a glossary file writes it.

    Parameters
    ----------
    name
        The shown name in the main language.
    definition
        What the term means.
    owner
        Who answers for the term; the asset header's `owner:` rules.
    synonyms
        Other words for it, in the main language.
    links
        Table → its linked columns; ``[]`` links the table itself.
    related
        Ids of related terms.
    """

    name: OneLine
    definition: Definition
    owner: str
    synonyms: tuple[OneLine, ...] = ()
    links: Annotated[dict[str, tuple[str, ...]], BeforeValidator(_links_shape)] = Field(
        default_factory=dict
    )
    related: tuple[str, ...] = ()

    @field_validator("owner")
    @classmethod
    def _owner_given(cls, value: str) -> str:
        """Refuse a blank owner.

        Parameters
        ----------
        value
            The owner.

        Returns
        -------
        str
            The stripped owner.

        Raises
        ------
        ValueError
            For a blank one.
        """
        if not value.strip():
            raise ValueError("must not be empty; name the team or person who answers for the term")
        return value.strip()

    @field_validator("synonyms")
    @classmethod
    def _synonyms(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        """Apply `synonym_problem` against the name.

        Parameters
        ----------
        value
            The synonyms.
        info
            Holds the validated name.

        Returns
        -------
        tuple of str
            `value`.
        """
        return _check_synonyms(value, info.data.get("name"))

    @field_validator("related")
    @classmethod
    def _related(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Apply the `related:` rules that need no other field.

        Parameters
        ----------
        value
            The ids.

        Returns
        -------
        tuple of str
            `value`.

        Raises
        ------
        ValueError
            For too many ids, an id that is not one, or one named twice.
        """
        if len(value) > MAX_RELATED:
            raise ValueError(
                f"names {len(value)} terms, and `related:` names at most {MAX_RELATED}; keep "
                "the closest ones"
            )
        for key in value:
            if term_id_problem(key) is not None:
                raise ValueError(f"`{key}` is not a term id; {TERM_ID_RULE}")
            if value.count(key) > 1:
                raise ValueError(f"names `{key}` twice; remove one")
        return value


class TermTexts(StrictModel):
    """One term's texts in one language other than the main one.

    Parameters
    ----------
    name
        The translated name.
    definition
        The translated definition.
    synonyms
        Synonyms in that language, besides the main language's.
    """

    name: OneLine | None = None
    definition: Definition | None = None
    synonyms: tuple[OneLine, ...] = ()

    @field_validator("synonyms")
    @classmethod
    def _synonyms(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        """Apply `synonym_problem` against the translated name.

        Parameters
        ----------
        value
            The synonyms.
        info
            Holds the validated name.

        Returns
        -------
        tuple of str
            `value`.
        """
        return _check_synonyms(value, info.data.get("name"))

    @model_validator(mode="after")
    def _not_empty(self) -> TermTexts:
        """Refuse an entry with no text.

        Returns
        -------
        TermTexts
            `self`.

        Raises
        ------
        ValueError
            For an entry with no name, definition or synonym.
        """
        if self.name is None and self.definition is None and not self.synonyms:
            raise ValueError("needs `name:`, `definition:` or `synonyms:`")
        return self


@dataclass(frozen=True)
class ParsedTerm:
    """One valid term.

    Parameters
    ----------
    key
        Its id.
    path
        The file that defines it.
    name
        Its name.
    definition
        Its definition.
    owner
        Its owner.
    synonyms
        Its main-language synonyms.
    links
        Table → its linked columns.
    related
        Ids of related terms.
    """

    key: str
    path: str
    name: str
    definition: str
    owner: str
    synonyms: tuple[str, ...]
    links: dict[str, tuple[str, ...]]
    related: tuple[str, ...]


@dataclass(frozen=True)
class Glossary:
    """A repository's parsed glossary.

    Parameters
    ----------
    terms
        Id → valid term, sorted by id.
    lines
        File path → the line of every key in it.
    invalid
        Ids whose term was refused; later checks do not report them again.
    """

    terms: dict[str, ParsedTerm]
    lines: dict[str, KeyLines] = field(default_factory=dict)
    invalid: frozenset[str] = frozenset()

    def line(self, key: str, *parts: object) -> int | None:
        """Find the line of a key of a valid term.

        Parameters
        ----------
        key
            The term id.
        *parts
            The keys below the term.

        Returns
        -------
        int or None
            The 1-based line.
        """
        term = self.terms[key]
        return self.lines[term.path].line([key, *parts])


def _no_check() -> None:
    """Check nothing: the default time check of `parse_glossary`."""


def _shape_issues(
    path: str, key: str, raw: Mapping[Any, Any], lines: KeyLines
) -> list[ProjectIssue]:
    """Refuse unknown fields and name the missing required ones.

    Parameters
    ----------
    path
        The file.
    key
        The term id.
    raw
        The term's mapping.
    lines
        The file's key lines.

    Returns
    -------
    list of ProjectIssue
        One per unknown field, then one per missing required field.
    """
    issues: list[ProjectIssue] = []
    for name in raw:
        if name in TERM_FIELDS:
            continue
        close = difflib.get_close_matches(str(name), TERM_FIELDS, n=1, cutoff=0.6)
        hint = f"; did you mean `{close[0]}`? A term has" if close else "; a term has"
        issues.append(
            ProjectIssue(
                path,
                lines.line([key, name]),
                f"{key}.{shown_key(name)}: is not a term field{hint} {_FIELD_LIST}",
            )
        )
    for name, what in _REQUIRED.items():
        if name not in raw:
            issues.append(ProjectIssue(path, lines.line([key]), f"{key}: needs `{name}:`, {what}"))
    return issues


def parse_glossary(
    found: tuple[DiscoveredGlossaryFile, ...] | None,
    *,
    check_time: Callable[[], None] = _no_check,
) -> tuple[Glossary | None, list[ProjectIssue]]:
    """Read each glossary file and apply the form rules.

    Parameters
    ----------
    found
        `discover_glossary`'s files; `None` for a repository without `glossary`.
    check_time
        Called before each file; raises when the deploy is past its deadline.

    Returns
    -------
    tuple
        The glossary (`None` exactly when `found` is) with every valid term, and every problem
        at its key's line: the YAML, a file that is not a mapping, a key that is not a term id,
        a term that is not a mapping, unknown and missing fields, each field's rules, a term that
        names itself in `related:`, an id defined in two files, and more than `MAX_TERMS` terms.
    """
    if found is None:
        return None, []
    terms: dict[str, ParsedTerm] = {}
    lines_of: dict[str, KeyLines] = {}
    invalid: set[str] = set()
    issues: list[ProjectIssue] = []
    defined_in: dict[str, str] = {}
    for item in found:
        check_time()
        try:
            raw, lines = load_yaml_with_lines(
                item.text,
                what=item.path,
                max_bytes=MAX_GLOSSARY_FILE_BYTES,
                max_entries=MAX_GLOSSARY_ENTRIES,
            )
        except YamlTooManyEntries as exc:
            issues.append(
                ProjectIssue(
                    item.path,
                    exc.line,
                    f"has more than {MAX_GLOSSARY_ENTRIES} keys and list items, and a glossary "
                    f"file has at most {MAX_GLOSSARY_ENTRIES}; split it into several files under "
                    "glossary/",
                )
            )
            continue
        except YamlError as exc:
            issues.append(ProjectIssue(item.path, exc.line, str(exc)))
            continue
        lines_of[item.path] = lines
        raw = {} if raw is None else raw
        if not isinstance(raw, dict):
            issues.append(
                ProjectIssue(
                    item.path,
                    1,
                    f"{item.path} must be a mapping of term ids to terms, such as "
                    "`net_revenue: {name: Net revenue, definition: …, owner: finance}`",
                )
            )
            continue
        for index, (key, value) in enumerate(raw.items()):
            problem = term_id_problem(key)
            if problem is not None:
                # The key as written (`yes`, `0x10`), not as YAML read it (`True`, `16`).
                written, line = lines.top[index] if index < len(lines.top) else (key, None)
                issues.append(
                    ProjectIssue(
                        item.path,
                        line,
                        f"`{shown_key(written)}` {problem}. Rename the key; the shown name goes "
                        "under `name:`",
                    )
                )
                continue
            if key in defined_in:
                invalid.add(key)
                issues.append(
                    ProjectIssue(
                        item.path,
                        lines.line([key]),
                        f"`{key}` is defined in {defined_in[key]} too; a term id is unique across "
                        "the glossary files. Rename one of them, or merge the two terms",
                    )
                )
                continue
            defined_in[key] = item.path
            if not isinstance(value, Mapping):
                invalid.add(key)
                issues.append(
                    ProjectIssue(
                        item.path,
                        lines.line([key]),
                        f"`{key}` must be a mapping of its fields, such as "
                        f"`{key}: {{name: …, definition: …, owner: …}}`",
                    )
                )
                continue
            shape = _shape_issues(item.path, key, value, lines)
            if shape:
                invalid.add(key)
                issues += shape
                continue
            try:
                spec = TermSpec.model_validate(value)
            except pydantic.ValidationError as exc:
                invalid.add(key)
                issues += pydantic_issues(
                    exc, path=item.path, line=None, prefix=(key,), lines=lines
                )
                continue
            if key in spec.related:
                invalid.add(key)
                issues.append(
                    ProjectIssue(
                        item.path,
                        lines.line([key, "related"]),
                        f"{key}.related: names the term itself; remove `{key}`",
                    )
                )
                continue
            terms[key] = ParsedTerm(
                key,
                item.path,
                spec.name,
                spec.definition,
                spec.owner,
                spec.synonyms,
                dict(spec.links),
                spec.related,
            )
    total = len(defined_in)
    if total > MAX_TERMS:
        return Glossary({}, lines_of, frozenset(defined_in)), [
            ProjectIssue(
                GLOSSARY_DIR,
                None,
                f"glossary/ defines {total} terms, and a repository has at most 2,000; move some "
                "to another repository's glossary, or remove the ones nobody uses",
            )
        ]
    ordered = dict(sorted(terms.items()))
    return Glossary(ordered, lines_of, frozenset(invalid)), issues
