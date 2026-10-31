"""Translation files: `translations/<language>.yaml`, keyed by table, checked against the project.

A file holds one language's texts for tables, their columns and their measures. Each text
translates one the asset header gives in the main language, so a deploy can refuse a text with
nothing to translate (a misspelt table, column or measure, Q1) and count what is covered (Q2).
Only the project's own assets are consulted, never the catalog, so no text here names a table
the deployer may not see.
"""

from __future__ import annotations

import contextlib
import difflib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import pydantic
from pydantic import AfterValidator, BeforeValidator, Field, model_validator

from lakana_core.languages import canonical_language, language_problem
from lakana_core.projects.asset_model import MAX_GRAIN_CHARS, ParsedAsset
from lakana_core.projects.discovery import (
    CONFIG_FILE,
    TRANSLATIONS_DIR,
    _decode,
    _display,
    _is_unsafe,
    _read,
)
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.project_config import (
    MAX_DESCRIPTION_CHARS,
    NEEDS_MAIN_LANGUAGE,
    ProjectConfig,
    StrictModel,
    checked_label,
    checked_text,
    pydantic_issues,
)
from lakana_core.projects.yaml_loader import (
    KeyLines,
    YamlError,
    YamlTooManyEntries,
    load_yaml_with_lines,
    shown_key,
)

MAX_LANGUAGES = 50
"""The most translation files a project may have."""

MAX_MISSING_SHOWN = 20
"""The most missing keys a strict-mode refusal names."""

MAX_FILE_ENTRIES = 50_000
"""The most keys and list items one file may hold, at any depth, counted before validation."""

_LINK = "symlinks are not allowed under translations/"
_NO_FOLDERS = (
    "translations/ holds files, not folders; put each language's texts in one file such as "
    "translations/de.yaml"
)
_ONLY_YAML = (
    "translations/ holds only one <language>.yaml file per language, such as "
    "translations/de.yaml; move this file elsewhere"
)
_NOT_A_FOLDER = (
    "translations must be a folder holding one <language>.yaml file per language, such as "
    "translations/de.yaml; move this file there under its language's name"
)
_FIELDS = ("label", "description")

MAX_FILE_ISSUES = 50
"""The most issues one check reports for one file; the rest are counted in a last issue."""


@dataclass(frozen=True)
class DiscoveredTranslation:
    """One translation file as discovery read it.

    Parameters
    ----------
    path
        Repository-relative path.
    language
        Its canonical tag, the file's name.
    text
        The decoded content.
    """

    path: str
    language: str
    text: str


def _name_issue(relative: str, stem: str) -> ProjectIssue | None:
    """Check a file's name as a language tag.

    Parameters
    ----------
    relative
        The file's path.
    stem
        Its name without ``.yaml``.

    Returns
    -------
    ProjectIssue or None
        The refusal with the fix, or `None` for a canonical tag.
    """
    problem = language_problem(stem)
    if problem is None:
        return None
    canonical = canonical_language(stem)
    if canonical is None:
        fix = "; name the file after the language of its texts, such as translations/de.yaml"
    else:
        fix = f": rename the file to translations/{canonical}.yaml"
    return ProjectIssue(relative, None, problem + fix)


def _kib(size: int) -> int:
    """Round a size up to whole KiB, so a size just over a limit never reads as the limit.

    Parameters
    ----------
    size
        Bytes.

    Returns
    -------
    int
        KiB, rounded up.
    """
    return -(-size // 1024)


def discover_translations(
    root: Path, *, limits: DeployLimits
) -> tuple[list[DiscoveredTranslation], list[ProjectIssue]]:
    """Find every translation file directly under ``translations/``.

    Parameters
    ----------
    root
        The extracted checkout.
    limits
        Supplies `translation_bytes`.

    Returns
    -------
    tuple of (list of DiscoveredTranslation, list of ProjectIssue)
        The files sorted by name, and every problem: a `translations` that is a link or not a
        folder, links, folders, other files, bad names, oversized or non-UTF-8 content, more
        than `MAX_LANGUAGES` entries, of any kind (those past it are not examined, so the issues
        stay few), and files past `translation_total_bytes` together (those are not read). A
        project without the folder has neither.
    """
    folder = root / TRANSLATIONS_DIR
    if folder.is_symlink():
        return [], [ProjectIssue(TRANSLATIONS_DIR, None, _LINK)]
    if not os.path.lexists(folder):
        return [], []
    if not folder.is_dir():
        return [], [ProjectIssue(TRANSLATIONS_DIR, None, _NOT_A_FOLDER)]
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except OSError as exc:
        return [], [ProjectIssue(TRANSLATIONS_DIR, None, f"cannot be read: {exc.strerror}")]
    found: list[DiscoveredTranslation] = []
    issues: list[ProjectIssue] = []
    files = 0
    total = 0
    for entry in entries:
        files += 1
        if files > MAX_LANGUAGES:
            continue
        relative = f"{TRANSLATIONS_DIR}/{entry.name}"
        if _is_unsafe(relative):
            issues.append(
                ProjectIssue(
                    _display(relative), None, "the path has a non-UTF-8 or control character"
                )
            )
            continue
        if entry.is_symlink():
            issues.append(ProjectIssue(relative, None, _LINK))
            continue
        if entry.is_dir(follow_symlinks=False):
            issues.append(ProjectIssue(relative, None, _NO_FOLDERS))
            continue
        stem, dot, suffix = entry.name.rpartition(".")
        if dot and suffix != "yaml" and suffix.lower() in ("yaml", "yml"):
            issues.append(
                ProjectIssue(
                    relative,
                    None,
                    f"a translation file ends in .yaml; rename it to translations/{stem}.yaml",
                )
            )
            continue
        if not dot or suffix != "yaml":
            issues.append(ProjectIssue(relative, None, _ONLY_YAML))
            continue
        named = _name_issue(relative, stem)
        if named is not None:
            issues.append(named)
            continue
        with contextlib.suppress(OSError):  # gone since the listing: `_read` below names it
            total += entry.stat(follow_symlinks=False).st_size
        if total > limits.translation_total_bytes:
            continue  # counted, never read: the refusal below names the total
        data = _read(Path(entry.path), relative, limits.translation_bytes, issues)
        text = _decode(data, relative, issues) if data is not None else None
        if text is not None:
            found.append(DiscoveredTranslation(relative, stem, text))
    if total > limits.translation_total_bytes:
        issues.append(
            ProjectIssue(
                TRANSLATIONS_DIR,
                None,
                f"translations/ holds {_kib(total)} KiB of translation files, and together they "
                f"may hold at most {_kib(limits.translation_total_bytes)} KiB; keep only the "
                "texts of tables, columns and measures the project builds",
            )
        )
    if files > MAX_LANGUAGES:
        issues.append(
            ProjectIssue(
                TRANSLATIONS_DIR,
                None,
                f"translations/ holds {files} files; a project has at most {MAX_LANGUAGES} "
                "languages besides its main language",
            )
        )
    return found, issues


def _description(value: str) -> str:
    """Apply a translated description's rules: not empty, at most 4,000 characters, safe.

    Parameters
    ----------
    value
        The description.

    Returns
    -------
    str
        The stripped description.
    """
    return checked_text(value, limit=MAX_DESCRIPTION_CHARS)


def _grain(value: str) -> str:
    """Apply `grain:`'s rules: one line, at most 300 characters, not empty, safe.

    Parameters
    ----------
    value
        The grain.

    Returns
    -------
    str
        The stripped grain.
    """
    return checked_text(value, limit=MAX_GRAIN_CHARS, one_line=True)


Label = Annotated[str, AfterValidator(checked_label)]
"""A translated label: the rules of every label."""

Description = Annotated[str, AfterValidator(_description)]
"""A translated description."""

Grain = Annotated[str, AfterValidator(_grain)]
"""A translated grain."""


class TextPair(StrictModel):
    """A column's or a measure's texts in one language.

    Parameters
    ----------
    label
        Its translated label.
    description
        Its translated description.
    """

    label: Label | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def _says_something(self) -> TextPair:
        """Refuse an entry with neither text.

        Returns
        -------
        TextPair
            `self`.

        Raises
        ------
        ValueError
            If both are missing.
        """
        if self.label is None and self.description is None:
            raise ValueError("needs `label:` or `description:`")
        return self


def _pairs_are_mappings(value: object) -> object:
    """Refuse a column or measure entry that is not a mapping, before pydantic names a class.

    Parameters
    ----------
    value
        The raw `columns:` or `measures:` value.

    Returns
    -------
    object
        `value`, for the type check that follows.

    Raises
    ------
    ValueError
        For a value that is not a mapping, or the first entry that is not one.
    """
    if not isinstance(value, Mapping):
        raise ValueError(
            "must be a mapping of names to their texts, such as "
            "`{store_id: {label: …, description: …}}`"
        )
    for name, texts in value.items():
        if not isinstance(texts, Mapping):
            raise ValueError(
                f"`{shown_key(name)}` must be a mapping such as `{{label: …, description: …}}`"
            )
    return value


TextPairs = Annotated[dict[str, TextPair], BeforeValidator(_pairs_are_mappings)]
"""Column or measure → its texts."""


class TableTranslation(StrictModel):
    """One table's texts in one language.

    Parameters
    ----------
    label
        The table's translated label.
    description
        Its translated description.
    grain
        Its translated grain.
    columns
        Column → its texts.
    measures
        Measure column → its texts.
    """

    label: Label | None = None
    description: Description | None = None
    grain: Grain | None = None
    columns: TextPairs = Field(default_factory=dict)
    measures: TextPairs = Field(default_factory=dict)


@dataclass(frozen=True)
class Translations:
    """One language's parsed file.

    Parameters
    ----------
    path
        Repository-relative path.
    language
        Its canonical tag.
    text
        The file's content, for key lines.
    tables
        Table → its texts.
    lines
        The line of every key, from the one parse of the file.
    invalid
        Tables whose entry was refused; strict mode does not report their texts as missing too.
    """

    path: str
    language: str
    text: str
    tables: dict[str, TableTranslation]
    lines: KeyLines = field(default_factory=KeyLines)
    invalid: frozenset[str] = frozenset()


class _FileIssues:
    """One check's issues for one file: at most `MAX_FILE_ISSUES`, then only a count.

    Parameters
    ----------
    path
        The file's repository-relative path.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.shown: list[ProjectIssue] = []
        self.more = 0

    def full(self) -> bool:
        """Say whether further issues are only counted.

        Returns
        -------
        bool
            True once `MAX_FILE_ISSUES` are kept.
        """
        return len(self.shown) >= MAX_FILE_ISSUES

    def add(self, issue: ProjectIssue) -> None:
        """Keep an issue, or count it once the file has its share.

        Parameters
        ----------
        issue
            The issue.
        """
        if self.full():
            self.more += 1
        else:
            self.shown.append(issue)

    def done(self) -> list[ProjectIssue]:
        """List the kept issues and, if some were only counted, a last one saying how many.

        Returns
        -------
        list of ProjectIssue
            The issues to report.
        """
        if not self.more:
            return self.shown
        rest = ProjectIssue(
            self.path,
            None,
            f"… and {self.more} more problems in {self.path}; fix the ones above and deploy "
            "again to see the rest",
        )
        return [*self.shown, rest]


def _no_check() -> None:
    """Check nothing: the default time check of `parse_translations`."""


def parse_translations(
    found: Sequence[DiscoveredTranslation],
    *,
    limits: DeployLimits,
    check_time: Callable[[], None] = _no_check,
) -> tuple[list[Translations], list[ProjectIssue]]:
    """Read each file's YAML and validate every table entry.

    Parameters
    ----------
    found
        `discover_translations`' files.
    limits
        Supplies `translation_bytes`.
    check_time
        Called before each file; raises when the deploy is past its deadline.

    Returns
    -------
    tuple of (list of Translations, list of ProjectIssue)
        Each readable file with its valid entries, and every problem, ``table.field: message`` at
        the key's line. A file with more than `MAX_FILE_ENTRIES` keys and list items is refused
        while it is parsed, before its values are built.
    """
    parsed: list[Translations] = []
    issues: list[ProjectIssue] = []
    for item in found:
        check_time()
        try:
            raw, lines = load_yaml_with_lines(
                item.text,
                what=item.path,
                max_bytes=limits.translation_bytes,
                max_entries=MAX_FILE_ENTRIES,
            )
        except YamlTooManyEntries as exc:
            issues.append(
                ProjectIssue(
                    item.path,
                    exc.line,
                    f"has more than {MAX_FILE_ENTRIES} keys, and a translation file has at most "
                    f"{MAX_FILE_ENTRIES}; keep only the texts of tables, columns and measures the "
                    "project builds",
                )
            )
            continue
        except YamlError as exc:
            issues.append(ProjectIssue(item.path, exc.line, str(exc)))
            continue
        raw = {} if raw is None else raw
        if not isinstance(raw, dict):
            issues.append(
                ProjectIssue(
                    item.path,
                    1,
                    f"{item.path} must be a mapping of table names to their texts, such as "
                    "`gold.fact_sales: {label: Ventes}`",
                )
            )
            continue
        tables: dict[str, TableTranslation] = {}
        invalid: set[str] = set()
        problems = _FileIssues(item.path)
        for index, (table, value) in enumerate(raw.items()):
            if not isinstance(table, str):
                # The key as written (`null`, `0x10`), not as YAML read it (`None`, `16`).
                written, line = lines.top[index] if index < len(lines.top) else (table, None)
                problems.add(
                    ProjectIssue(
                        item.path,
                        line,
                        f"`{shown_key(written)}` is not a table name; a key is a table's dotted "
                        "name, such as `gold.fact_sales`",
                    )
                )
                continue
            if value is not None and not isinstance(value, Mapping):
                invalid.add(table)
                problems.add(
                    ProjectIssue(
                        item.path,
                        lines.line([table]),
                        f"`{shown_key(table)}` must be a mapping of its texts, such as "
                        f"`{shown_key(table)}: {{label: …, description: …, columns: …}}`",
                    )
                )
                continue
            try:
                tables[table] = TableTranslation.model_validate({} if value is None else value)
            except pydantic.ValidationError as exc:
                invalid.add(table)
                for issue in pydantic_issues(
                    exc, path=item.path, line=None, prefix=(table,), lines=lines
                ):
                    problems.add(issue)
        issues += problems.done()
        parsed.append(
            Translations(item.path, item.language, item.text, tables, lines, frozenset(invalid))
        )
    return parsed, issues


def _has(text: str | None) -> bool:
    """Say whether a header text is there to translate (blank counts as none, as it shows).

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    bool
        True for a non-blank string.
    """
    return text is not None and bool(text.strip())


def header_texts(asset: ParsedAsset) -> list[str]:
    """List the keys of every text an asset's header gives, in header order.

    Parameters
    ----------
    asset
        The asset; an artifact asset has none (the catalog shows no texts for it).

    Returns
    -------
    list of str
        Keys as a translation file nests them, such as ``gold.t.columns.c.label``.
    """
    if asset.output == "artifact":
        return []
    meta, table = asset.metadata, asset.table
    keys = [f"{table}.{f}" for f in ("label", "description", "grain") if _has(getattr(meta, f))]
    for column, texts in meta.columns.items():
        keys += [f"{table}.columns.{column}.{f}" for f in _FIELDS if _has(getattr(texts, f))]
    for column, spec in meta.measures.items():
        keys += [f"{table}.measures.{column}.{f}" for f in _FIELDS if _has(getattr(spec, f))]
    return keys


def translated_texts(table: str, entry: TableTranslation) -> list[str]:
    """List the keys of every text one table's entry translates.

    Parameters
    ----------
    table
        The table.
    entry
        Its texts in one language.

    Returns
    -------
    list of str
        Keys in `header_texts`' form.
    """
    keys = [f"{table}.{f}" for f in ("label", "description", "grain") if getattr(entry, f)]
    for part, pairs in (("columns", entry.columns), ("measures", entry.measures)):
        for name, pair in pairs.items():
            keys += [f"{table}.{part}.{name}.{f}" for f in _FIELDS if getattr(pair, f)]
    return keys


def _unknown_table(table: str, known: Sequence[str]) -> str:
    """Word the refusal of a table the project does not build, with a close name if there is one.

    Parameters
    ----------
    table
        The key as written.
    known
        The project's own table names.

    Returns
    -------
    str
        The text.
    """
    close = difflib.get_close_matches(table, known, n=1, cutoff=0.8)
    hint = f"; did you mean `{close[0]}`? Fix" if close else "; fix"
    return (
        f"`{shown_key(table)}` is not a table this project builds{hint} the name, or remove "
        "the entry"
    )


def _entry_issues(
    t: Translations, table: str, entry: TableTranslation, asset: ParsedAsset
) -> list[ProjectIssue]:
    """Check one table's entry against its asset's header.

    Parameters
    ----------
    t
        The file.
    table
        The table.
    entry
        Its texts.
    asset
        The asset that builds it.

    Returns
    -------
    list of ProjectIssue
        A column or measure the header does not declare, and a text without its main-language
        text, each at its key's line.
    """
    meta, source = asset.metadata, asset.path
    issues: list[ProjectIssue] = []

    def at(*parts: str) -> int | None:
        """Find the line of a key below the table's.

        Parameters
        ----------
        *parts
            The keys below the table.

        Returns
        -------
        int or None
            Its 1-based line.
        """
        return t.lines.line([table, *parts])

    for kind in ("label", "description", "grain"):
        if getattr(entry, kind) is not None and not _has(getattr(meta, kind)):
            issues.append(
                ProjectIssue(
                    t.path,
                    at(kind),
                    f"the header of `{table}` has no {kind} to translate; add `{kind}:` in "
                    f"{source}, or remove this text",
                )
            )
    for column, pair in entry.columns.items():
        texts = meta.columns.get(column)
        if texts is None:
            issues.append(
                ProjectIssue(
                    t.path,
                    at("columns", column),
                    f"`{shown_key(column)}` is not a column the header of `{table}` describes "
                    f"(`columns:` in {source}); fix the name, or describe the column there first",
                )
            )
            continue
        for kind in _FIELDS:
            if getattr(pair, kind) is not None and not _has(getattr(texts, kind)):
                issues.append(
                    ProjectIssue(
                        t.path,
                        at("columns", column, kind),
                        f"the header of `{table}` gives column `{shown_key(column)}` no {kind} to "
                        f"translate; add `{kind}:` for it under `columns:` in {source}, or "
                        "remove this text",
                    )
                )
    for column, pair in entry.measures.items():
        spec = meta.measures.get(column)
        if spec is None:
            issues.append(
                ProjectIssue(
                    t.path,
                    at("measures", column),
                    f"`{shown_key(column)}` is not a measure of `{table}` (`measures:` in "
                    f"{source}); fix the name, or remove the entry",
                )
            )
            continue
        for kind in _FIELDS:
            if getattr(pair, kind) is not None and not _has(getattr(spec, kind)):
                issues.append(
                    ProjectIssue(
                        t.path,
                        at("measures", column, kind),
                        f"measure `{shown_key(column)}` of `{table}` has no {kind} to translate; "
                        f"add `{kind}:` to it under `measures:` in {source}, or remove this text",
                    )
                )
    return issues


def _missing_issue(t: Translations, missing: list[str]) -> ProjectIssue:
    """Word strict mode's refusal for one language.

    Parameters
    ----------
    t
        The file of that language.
    missing
        The missing keys, sorted.

    Returns
    -------
    ProjectIssue
        The issue at the file, naming at most `MAX_MISSING_SHOWN` keys.
    """
    named = ", ".join(missing[:MAX_MISSING_SHOWN])
    rest = len(missing) - MAX_MISSING_SHOWN
    more = f" … and {rest} more" if rest > 0 else ""
    verb = "is" if len(missing) == 1 else "are"
    return ProjectIssue(
        t.path,
        None,
        f"translations.require in lakana.yaml needs a `{t.language}` text for every header text, "
        f"and {len(missing)} {verb} missing: {named}{more}. Add them to {t.path}, or remove "
        f"`{t.language}` from translations.require.",
    )


def translation_issues(
    config: ProjectConfig, assets: Sequence[ParsedAsset], translations: Sequence[Translations]
) -> list[ProjectIssue]:
    """Check the translation files against `lakana.yaml` and the project's assets.

    Parameters
    ----------
    config
        The whole `lakana.yaml`.
    assets
        The parsed assets.
    translations
        The parsed files.

    Returns
    -------
    list of ProjectIssue
        A missing `main_language`, a file of the main language, an unknown or artifact table,
        the per-entry problems, and strict mode's gaps and missing files.
    """
    main = config.main_language
    require = config.translations.require
    if not translations and not require:
        return []
    issues: list[ProjectIssue] = []
    if main is None and translations and not require:
        issues.append(ProjectIssue(CONFIG_FILE, None, NEEDS_MAIN_LANGUAGE))
    by_table = {asset.table: asset for asset in assets}
    every = sorted({key for asset in assets for key in header_texts(asset)})
    files = {t.language: t for t in translations}
    known = sorted(by_table)
    for t in translations:
        if t.language == main:
            issues.append(
                ProjectIssue(
                    t.path,
                    None,
                    f"`{main}` is the project's main language (`main_language:` in lakana.yaml); "
                    "its texts belong in the asset headers. Move them there, or remove the file",
                )
            )
            continue
        problems = _FileIssues(t.path)
        for table, entry in t.tables.items():
            asset = by_table.get(table)
            line = t.lines.line([table])
            if asset is None:
                if problems.full():  # only counted: spare the close-name search
                    problems.more += 1
                else:
                    problems.add(ProjectIssue(t.path, line, _unknown_table(table, known)))
            elif asset.output == "artifact":
                problems.add(
                    ProjectIssue(
                        t.path,
                        line,
                        f"`{table}` is an artifact, which has no catalog texts to translate; "
                        "remove the entry",
                    )
                )
            else:
                for issue in _entry_issues(t, table, entry, asset):
                    problems.add(issue)
        issues += problems.done()
    for language in require:
        if language == main:
            continue
        t = files.get(language)
        if t is None:
            issues.append(
                ProjectIssue(
                    CONFIG_FILE,
                    None,
                    f"translations.require lists `{language}`, but there is no "
                    f"translations/{language}.yaml; add the file with a text for every header "
                    f"text, or remove `{language}` from the list",
                )
            )
            continue
        done = {key for table, entry in t.tables.items() for key in translated_texts(table, entry)}
        refused = tuple(f"{table}." for table in t.invalid)  # reported already, not twice
        missing = [key for key in every if key not in done and not key.startswith(refused)]
        if missing:
            issues.append(_missing_issue(t, missing))
    return issues


def coverage_hints(
    config: ProjectConfig, assets: Sequence[ParsedAsset], translations: Sequence[Translations]
) -> tuple[str, ...]:
    """Say, per file, how many header texts it translates; the deploy prints these as hints.

    Parameters
    ----------
    config
        The whole `lakana.yaml`, for the main language.
    assets
        The parsed assets.
    translations
        The parsed, validated files.

    Returns
    -------
    tuple of str
        One hint per file, by language.
    """
    every = {key for asset in assets for key in header_texts(asset)}
    hints: list[str] = []
    for t in sorted(translations, key=lambda t: t.language):
        done = {k for table, e in t.tables.items() for k in translated_texts(table, e)} & every
        hint = f"{t.path}: {len(done)} of {len(every)} texts translated"
        rest = len(every) - len(done)
        if rest:
            hint += f"; the other {rest} show in `{config.main_language}`"
        hints.append(hint)
    return tuple(hints)


def translation_entries(
    translations: Sequence[Translations],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Build the manifest form of every table's translations.

    Parameters
    ----------
    translations
        The parsed, validated files.

    Returns
    -------
    dict
        Table → language → ``{label, description, grain, columns, measures}``, absent texts and
        empty sections left out, languages sorted.
    """
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for t in sorted(translations, key=lambda t: t.language):
        for table, entry in sorted(t.tables.items()):
            form: dict[str, Any] = entry.model_dump(
                exclude_none=True, exclude={"columns", "measures"}
            )
            for part in ("columns", "measures"):
                pairs = {
                    name: pair.model_dump(exclude_none=True)
                    for name, pair in sorted(getattr(entry, part).items())
                }
                if pairs:
                    form[part] = pairs
            result.setdefault(table, {})[t.language] = form
    return result
