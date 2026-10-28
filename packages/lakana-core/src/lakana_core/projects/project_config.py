"""The strict `lakana.yaml` model and its per-environment deep merge."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfoNotFoundError

import pydantic
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
)

from lakana_core.errors import ValidationError
from lakana_core.identifiers import NOTIFY_FORMATS, is_secret_name, shown, validate_path_component
from lakana_core.jobs.cron import validate_cron_can_fire, validate_cron_syntax
from lakana_core.jobs.tz import load_zone
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.yaml_loader import YamlError, key_line, load_yaml

Materialize = Literal["table", "view", "incremental"]
"""How an asset is materialized; mapped onto M5a write modes in `manifest`."""

RequiredField = Literal["description", "owner", "tags"]
"""A front-matter field `requirements.fields` may demand."""

_DURATION = re.compile(r"\A(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?\Z")
_DURATION_HINT = "a duration is a whole number of seconds or like '90s', '5m', '1h30m'"


def parse_duration(value: object) -> int:
    """Turn a duration from YAML into seconds.

    Parameters
    ----------
    value
        An integer number of seconds, or a string of ``<n>h``, ``<n>m`` and
        ``<n>s`` parts in that order.

    Returns
    -------
    int
        Seconds.

    Raises
    ------
    ValueError
        For anything else, including booleans, floats and negative numbers.
        Pydantic reports it as a field error.
    """
    if isinstance(value, bool):
        raise ValueError(_DURATION_HINT)
    if isinstance(value, int):
        if value < 0:
            raise ValueError(_DURATION_HINT)
        return value
    if isinstance(value, str) and value:
        match = _DURATION.match(value)
        if match is not None:
            hours, minutes, seconds = (int(part or 0) for part in match.groups())
            return hours * 3600 + minutes * 60 + seconds
    raise ValueError(_DURATION_HINT)


MAX_RETRIES = 10_000
"""The largest `retries` a repository may ask for; keeps every count printable."""

MAX_DURATION_SECONDS = 31_536_000
"""The largest duration a repository may ask for: one year."""

Duration = Annotated[int, BeforeValidator(parse_duration), Field(ge=0, le=MAX_DURATION_SECONDS)]
"""A duration of zero or more seconds (`retry_delay`)."""

PositiveDuration = Annotated[
    int, BeforeValidator(parse_duration), Field(gt=0, le=MAX_DURATION_SECONDS)
]
"""A duration of at least one second (`timeout`)."""


class StrictModel(BaseModel):
    """Base for every repository-content model: unknown keys are errors, instances frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


_IDENTIFIER_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")
_FREE_TEXT_ALLOWED_CONTROLS = ("\n", "\t")
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e") | frozenset(
    chr(code) for code in range(0x2066, 0x206A)
)

IDENTIFIER_MESSAGE = "must not contain a control, format or surrogate character or a line separator"
FREE_TEXT_MESSAGE = (
    "must not contain a surrogate, a control character other than a newline or tab, "
    "a bidirectional control or a line separator"
)


def unsafe_identifier(text: str) -> bool:
    """Say whether an identifier-like string cannot be stored or rendered faithfully.

    Parameters
    ----------
    text
        An owner, tag, key or table name from a repository file.

    Returns
    -------
    bool
        True if it has a control, format or surrogate character or a line or
        paragraph separator, which could forge lines in rendered issues.
    """
    return any(unicodedata.category(char) in _IDENTIFIER_CATEGORIES for char in text)


def unsafe_free_text(text: str) -> bool:
    """Say whether a description is unsafe to store or render.

    Newlines, tabs, ZWNJ and ZWJ (emoji sequences, Persian) are fine.

    Parameters
    ----------
    text
        A description from a repository file.

    Returns
    -------
    bool
        True if it has a surrogate, a control character other than a newline
        or tab, a bidirectional control or a line or paragraph separator.
    """
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc":
            if char not in _FREE_TEXT_ALLOWED_CONTROLS:
                return True
        elif category in ("Cs", "Zl", "Zp") or char in _BIDI_CONTROLS:
            return True
    return False


class SafeTextModel(StrictModel):
    """A model whose `owner`, `tags` and `description` fields obey the character rules."""

    @field_validator("owner", check_fields=False)
    @classmethod
    def _owner_is_safe(cls, value: str | None) -> str | None:
        """Refuse an owner that could forge lines in rendered output.

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
            raise ValueError(f"owner {IDENTIFIER_MESSAGE}")
        return value

    @field_validator("tags", check_fields=False)
    @classmethod
    def _tags_are_safe(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Refuse a tag that could forge lines in rendered output.

        Parameters
        ----------
        value
            The field's value.

        Returns
        -------
        tuple of str or None
            `value`.

        Raises
        ------
        ValueError
            If a tag has an unsafe character.
        """
        if value is not None and any(unsafe_identifier(tag) for tag in value):
            raise ValueError(f"a tag {IDENTIFIER_MESSAGE}")
        return value

    @field_validator("description", check_fields=False)
    @classmethod
    def _description_is_safe(cls, value: str | None) -> str | None:
        """Refuse a description with an unsafe character.

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
        if value is not None and unsafe_free_text(value):
            raise ValueError(f"description {FREE_TEXT_MESSAGE}")
        return value


class AssetDefaults(SafeTextModel):
    """Metadata applied to every asset unless its front-matter overrides it.

    Parameters
    ----------
    description
        Default description.
    owner
        Default owner.
    tags
        Default tags.
    materialize
        Default materialization; ``table`` when nothing sets it.
    retries
        Default number of retries after a failed attempt.
    retry_delay
        Default first retry delay; doubled per attempt.
    timeout
        Default wall-clock limit of one attempt.
    """

    description: str | None = None
    owner: str | None = None
    tags: tuple[str, ...] | None = None
    materialize: Materialize | None = None
    retries: int | None = Field(default=None, ge=0, le=MAX_RETRIES, strict=True)
    retry_delay: Duration | None = None
    timeout: PositiveDuration | None = None


class Requirements(StrictModel):
    """What every asset must satisfy for a deploy to pass.

    Parameters
    ----------
    fields
        Front-matter fields each asset must have, from its own block or
        from `defaults`.
    column_docs
        Every column of an explicit projection must be documented.
    """

    fields: tuple[RequiredField, ...] = ()
    column_docs: StrictBool = False


class ChecksDefaults(StrictModel):
    """What every data quality check does unless its own block says otherwise.

    Parameters
    ----------
    on_failure
        ``block`` (the default) or ``warn``. ``keep`` is not allowed here: it leaves failing
        data where readers find it, so it is set on a single check, never for all of them.
    """

    on_failure: Literal["block", "warn"] = "block"


def _cron_syntax(value: str) -> str:
    """Refuse a cron expression that is not five standard fields in range, or never fires.

    Parameters
    ----------
    value
        The ``cron`` value.

    Returns
    -------
    str
        `value`, unchanged.
    """
    validate_cron_syntax(value)
    validate_cron_can_fire(value)
    return value


def _known_time_zone(value: str) -> str:
    """Refuse a time zone the server does not know.

    Parameters
    ----------
    value
        The ``timezone`` value.

    Returns
    -------
    str
        `value`, unchanged.

    Raises
    ------
    ValueError
        With a hint to use an IANA name, if ``tzdata`` does not know it. A zone file that cannot
        be read is a broken install and is not caught.
    """
    try:
        load_zone(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"'{shown(value)}' is not a time zone this server knows. Use an IANA name such as "
            "'Europe/Berlin', 'America/New_York' or 'UTC'"
        ) from exc
    return value


def _job_name(value: str) -> str:
    """Refuse a job name that could not also be an asset name.

    Parameters
    ----------
    value
        A key of ``jobs``.

    Returns
    -------
    str
        `value`, unchanged.

    Raises
    ------
    ValueError
        If it is empty, has a dot, a slash or whitespace only, or has an unsafe character.
        Pydantic reports only `ValueError`, so core's `ValidationError` is converted here.
    """
    try:
        validate_path_component(value, what="A job name")
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    if unsafe_identifier(value):
        raise ValueError(f"a job name {IDENTIFIER_MESSAGE}")
    return value


CronExpression = Annotated[str, AfterValidator(_cron_syntax)]
"""Five standard cron fields, or one of the five ``@`` shorthands."""

TimeZoneName = Annotated[str, AfterValidator(_known_time_zone)]
"""An IANA time zone name that ``tzdata`` knows."""

JobName = Annotated[str, AfterValidator(_job_name)]
"""A job's name; the rules of an asset's name segment."""


class ScheduleConfig(StrictModel):
    """When a job starts by itself.

    Parameters
    ----------
    cron
        Five standard fields (minute, hour, day of month, month, day of week) or an ``@hourly``,
        ``@daily``, ``@weekly``, ``@monthly`` or ``@yearly`` shorthand. No seconds.
    timezone
        The IANA zone ``cron`` is read in; ``UTC`` when left out.
    """

    cron: CronExpression
    timezone: TimeZoneName = "UTC"


def _secret_name(value: str) -> str:
    """Refuse a ``webhook`` that could not be the name of a secret.

    Parameters
    ----------
    value
        The ``webhook`` value; an empty one stands for a missing key.

    Returns
    -------
    str
        `value`, unchanged.

    Raises
    ------
    ValueError
        With the next step, if the value is empty, longer than 255 characters or has a space,
        a control character or ``:``.
    """
    if not is_secret_name(value):
        what = (
            "is missing"
            if value == ""
            else f"'{shown(value)}' is not a secret name: a name has 1 to 255 characters and no "
            "space, control character or ':'"
        )
        raise ValueError(
            f"`webhook` {what}. Give the name of a secret of the environment that holds the "
            "webhook URL, for example `webhook: team-alerts`; create it with `lakana secret "
            'create team-alerts --env ENV --values-stdin` and a JSON object with a "url"'
        )
    return value


WebhookSecret = Annotated[str, AfterValidator(_secret_name)]
"""The name of a secret of the environment; its value holds the webhook URL."""

SUPPORTED_TASK_PYTHONS = ("3.11", "3.12", "3.13", "3.14")
"""The Python versions Lakana ships a task image for (`lakana.yaml`'s `python:`)."""


def _task_python(value: object) -> object:
    """Refuse a `python:` that names no task image, before pydantic reads it as text.

    Parameters
    ----------
    value
        The raw YAML value.

    Returns
    -------
    object
        `value`, when it is one of `SUPPORTED_TASK_PYTHONS`.

    Raises
    ------
    ValueError
        For a number (YAML reads ``3.10`` as 3.1) or any other value.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raise ValueError(
            f'write the version in quotes, such as python: "3.12": YAML reads {value} as a number'
        )
    if not isinstance(value, str) or value not in SUPPORTED_TASK_PYTHONS:
        raise ValueError(
            'must be one of "3.11", "3.12", "3.13", "3.14" (the Python versions Lakana ships a '
            f"task image for), in quotes; got '{shown(str(value))}'."
        )
    return value


TaskPython = Annotated[str, BeforeValidator(_task_python)]
"""A Python version Lakana ships a task image for."""


def _notify_format(value: str) -> str:
    """Refuse a ``format`` Lakana does not send.

    Parameters
    ----------
    value
        The ``format`` value.

    Returns
    -------
    str
        `value`, unchanged.

    Raises
    ------
    ValueError
        With the formats there are and an example, if `value` is not one of them.
    """
    if value not in NOTIFY_FORMATS:
        raise ValueError(
            f"'{shown(value)}' is not a message format. Use one of json, slack or teams, "
            "for example `format: slack`"
        )
    return value


NotifyFormat = Annotated[str, AfterValidator(_notify_format)]
"""The message formats a webhook can receive."""


class NotifyConfig(StrictModel):
    """Where a job reports a failed or recovered run.

    Parameters
    ----------
    webhook
        The name of a secret in the environment. The secret is a JSON object with a ``url`` and,
        to sign the ``json`` format, a ``signing_key``; the URL never appears in the
        repository.
    format
        ``json`` (a versioned object, signed when the secret has a key), ``slack`` (a plain text
        message that other chat tools also accept) or ``teams``.
    """

    webhook: WebhookSecret = Field(default="", validate_default=True)
    format: NotifyFormat = "json"


class JobConfig(StrictModel):
    """A named selection of assets, optionally started on a schedule or after each deploy.

    Parameters
    ----------
    select
        Shell globs over dotted table names, as ``lakana run --select``; at least one.
    upstream
        Add every ancestor of the matched assets, as ``lakana run --upstream``.
    downstream
        Add every descendant of the matched assets, as ``lakana run --downstream``.
    schedule
        When it starts by itself; none means it only starts by hand or through the API.
    notify
        Where a failed or recovered run is reported; none means nobody is told.
    on_deploy
        Start the job after each deploy that changes the project version of the environment,
        as the principal that deployed; off by default.
    """

    select: Annotated[list[str], Field(min_length=1)]
    upstream: StrictBool = False
    downstream: StrictBool = False
    schedule: ScheduleConfig | None = None
    notify: NotifyConfig | None = None
    on_deploy: StrictBool = False


class ScheduleOverride(StrictModel):
    """The part of a schedule an environment changes; the rest is kept.

    Only the fields the file sets are merged, so the defaults here are never used.

    Parameters
    ----------
    cron
        A new cron expression.
    timezone
        A new time zone.
    """

    cron: CronExpression = "* * * * *"
    timezone: TimeZoneName = "UTC"


class NotifyOverride(StrictModel):
    """The part of a notification an environment changes; the rest is kept.

    Only the fields the file sets are merged, so the defaults here are never used.

    Parameters
    ----------
    webhook
        Another secret.
    format
        Another message format.
    """

    webhook: WebhookSecret | None = None
    format: NotifyFormat = "json"


class JobOverride(StrictModel):
    """The part of a job an environment changes; the rest is kept.

    Only the fields the file sets are merged, so the defaults here are never used. ``schedule:
    null`` and ``notify: null`` remove the schedule or the notification in that environment.

    Parameters
    ----------
    select
        A new selection.
    upstream
        A new ``upstream``.
    downstream
        A new ``downstream``.
    schedule
        Changes to the schedule, or ``null`` for none.
    notify
        Changes to the notification, or ``null`` for none.
    on_deploy
        Whether the job starts after each deploy in this environment.
    """

    select: Annotated[list[str], Field(min_length=1)] = []
    upstream: StrictBool = False
    downstream: StrictBool = False
    schedule: ScheduleOverride | None = None
    notify: NotifyOverride | None = None
    on_deploy: StrictBool = False


class ProjectSettings(StrictModel):
    """The sections an environment override may set.

    Parameters
    ----------
    defaults
        Asset defaults.
    requirements
        Validation requirements.
    checks
        Defaults for the project's data quality checks.
    properties
        Free-form string map, stored for M5c/M5d.
    """

    defaults: AssetDefaults = AssetDefaults()
    requirements: Requirements = Requirements()
    checks: ChecksDefaults = ChecksDefaults()
    properties: dict[str, str] = {}


class EnvironmentOverride(ProjectSettings):
    """A partial `ProjectSettings` deep-merged over the base for one environment.

    Parameters
    ----------
    jobs
        Changes to jobs the file declares at the top level. An override cannot add a job.
    """

    jobs: dict[JobName, JobOverride] = {}


class ResolvedProjectConfig(ProjectSettings):
    """`lakana.yaml` as it applies to one environment, stored in the manifest.

    Parameters
    ----------
    repo
        The repository name the CLI uses when none is given.
    jobs
        The jobs, by name.
    """

    repo: str | None = None
    jobs: dict[JobName, JobConfig] = {}


class ProjectConfig(ResolvedProjectConfig):
    """The whole `lakana.yaml`.

    Parameters
    ----------
    environments
        Per-environment overrides. An override for an environment that does
        not exist is allowed; only the target's is applied.
    python
        The Python version the project's Python tasks run on, one of
        `SUPPORTED_TASK_PYTHONS`; `None` runs them on the worker's own. Top-level
        only: it chooses the task image, so no environment may change it.
    """

    environments: dict[str, EnvironmentOverride] = {}
    python: TaskPython | None = None

    @field_validator("repo")
    @classmethod
    def _repo_is_a_name(cls, value: str | None) -> str | None:
        """Refuse a `repo` that could not be a registered repository's name.

        Parameters
        ----------
        value
            The `repo:` value.

        Returns
        -------
        str or None
            The value, unchanged.

        Raises
        ------
        ValueError
            If the name is not a valid path component. Pydantic turns only
            `ValueError` into a field error, so core's `ValidationError` is
            converted here.
        """
        if value is not None:
            try:
                validate_path_component(value, what="Repository name")
            except ValidationError as exc:
                raise ValueError(str(exc)) from exc
        return value

    def resolved_for(self, environment: str) -> ResolvedProjectConfig:
        """Apply `environment`'s override, if any, over the base.

        Parameters
        ----------
        environment
            The target environment's name.

        Returns
        -------
        ResolvedProjectConfig
            Mappings merged key by key; lists and scalars replaced.

        Raises
        ------
        ProjectValidationError
            If the merge does not validate, for example a schedule left without a `cron`.
        """
        base = self.model_dump(exclude={"environments", "python"})
        override = self.environments.get(environment)
        if override is not None:
            base = _deep_merge(base, override.model_dump(exclude_unset=True))
        try:
            return ResolvedProjectConfig.model_validate(base)
        except pydantic.ValidationError as exc:
            raise ProjectValidationError(
                pydantic_issues(exc, path="lakana.yaml", line=None)
            ) from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge `override` into a copy of `base`, recursing into mappings.

    Parameters
    ----------
    base
        The base mapping.
    override
        The mapping whose values win.

    Returns
    -------
    dict
        A new mapping.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_source(text: str) -> str:
    """Drop a UTF-8 BOM and turn CRLF line endings into LF.

    Parameters
    ----------
    text
        File content as decoded.

    Returns
    -------
    str
        The content with Unix line endings.
    """
    return text.removeprefix("﻿").replace("\r\n", "\n")


def _loc_part(part: object) -> str:
    """Render one part of an error location on a single line.

    Parameters
    ----------
    part
        A field name, an index or a dict key from the document.

    Returns
    -------
    str
        `part`, ASCII-escaped when it has a non-printable character, so a key
        from the repository cannot start a second line.
    """
    text = str(part)
    return text if text.isprintable() else ascii(text)[1:-1]


def pydantic_issues(
    exc: pydantic.ValidationError, *, path: str, line: int | None, text: str | None = None
) -> list[ProjectIssue]:
    """Turn a pydantic error into one issue per failing field.

    Parameters
    ----------
    exc
        The pydantic error.
    path
        The file the document came from.
    line
        The line to attach, if the document has one.
    text
        The YAML the model was built from. With it, an error without a `line` gets the line
        of the deepest key its location reaches.

    Returns
    -------
    list of ProjectIssue
        ``<dotted field>: <message>`` per error.
    """
    issues = []
    for error in exc.errors():
        parts = [part for part in error["loc"] if part != "[key]"]
        message = error["msg"]
        if error["type"] == "value_error":  # pydantic prefixes what a validator raised
            message = message.removeprefix("Value error, ")
        where = line
        if where is None and text is not None:
            where = key_line(text, parts)
        issues.append(
            ProjectIssue(
                path=path,
                line=where,
                message=f"{'.'.join(_loc_part(part) for part in parts)}: {message}",
            )
        )
    return issues


def _unknown_job_issues(raw: dict[Any, Any], *, text: str, path: str) -> list[ProjectIssue]:
    """Refuse an environment override of a job the file does not declare.

    Works on the document as parsed, so it is reported together with the other problems. Anything
    but a mapping is left to pydantic.

    Parameters
    ----------
    raw
        The parsed document.
    text
        Its YAML, for the line.
    path
        The file's path, for issues.

    Returns
    -------
    list of ProjectIssue
        One per overridden job that has no top-level declaration.
    """
    declared = raw.get("jobs")
    names = sorted(str(name) for name in declared) if isinstance(declared, dict) else []
    known = ", ".join(names)
    exists = f"the jobs in this file are: {known}" if known else "this file declares no jobs"
    environments = raw.get("environments")
    issues = []
    for environment, override in (environments if isinstance(environments, dict) else {}).items():
        overridden = override.get("jobs") if isinstance(override, dict) else None
        for name in overridden if isinstance(overridden, dict) else ():
            if not isinstance(declared, dict) or name not in declared:
                where = ("environments", environment, "jobs", name)
                issues.append(
                    ProjectIssue(
                        path,
                        key_line(text, where),
                        f"environments.{_loc_part(environment)}.jobs.{_loc_part(name)}: an "
                        f"environment can only change a job declared under `jobs:`; it cannot "
                        f"add one, and {exists}. Declare the job at the top level first.",
                    )
                )
    return issues


def _schedule_without_cron_issues(
    config: ProjectConfig, *, text: str, path: str
) -> list[ProjectIssue]:
    """Refuse an override that gives a job a schedule but no `cron`.

    Parameters
    ----------
    config
        The validated file.
    text
        Its YAML, for the line.
    path
        The file's path, for issues.

    Returns
    -------
    list of ProjectIssue
        One per override whose schedule has no `cron` of its own while the job has none to keep.
    """
    issues = []
    for environment, override in config.environments.items():
        for name, job in override.jobs.items():
            base = config.jobs.get(name)
            schedule = job.schedule
            if (
                base is not None
                and base.schedule is None
                and schedule is not None
                and "cron" not in schedule.model_fields_set
            ):
                issues.append(
                    ProjectIssue(
                        path,
                        key_line(text, ("environments", environment, "jobs", name, "schedule")),
                        f"environments.{_loc_part(environment)}.jobs.{_loc_part(name)}.schedule: "
                        f"job {_loc_part(name)!r} has no schedule to change, so this one needs a "
                        "`cron`. Add `cron:` here, or give the job a schedule under `jobs:`.",
                    )
                )
    return issues


def _notify_without_webhook_issues(
    config: ProjectConfig, *, text: str, path: str
) -> list[ProjectIssue]:
    """Refuse an override that gives a job a notification but no `webhook`.

    Parameters
    ----------
    config
        The validated file.
    text
        Its YAML, for the line.
    path
        The file's path, for issues.

    Returns
    -------
    list of ProjectIssue
        One per override whose notification has no `webhook` of its own while the job has none
        to keep.
    """
    issues = []
    for environment, override in config.environments.items():
        for name, job in override.jobs.items():
            base = config.jobs.get(name)
            notify = job.notify
            if (
                base is not None
                and base.notify is None
                and notify is not None
                and "webhook" not in notify.model_fields_set
            ):
                issues.append(
                    ProjectIssue(
                        path,
                        key_line(text, ("environments", environment, "jobs", name, "notify")),
                        f"environments.{_loc_part(environment)}.jobs.{_loc_part(name)}.notify: "
                        f"job {_loc_part(name)!r} has no notification to change, so this one "
                        "needs a `webhook`. Add `webhook:` here, or give the job a `notify:` "
                        "under `jobs:`.",
                    )
                )
    return issues


_KEEP_AS_DEFAULT = (
    "must be block or warn (got 'keep'). `keep` leaves failing data where readers find it, "
    "so it can only be set on a check: put `on_failure: keep` in that check's /* lakana */ block."
)


def _refuse_keep_as_default(raw: dict[Any, Any], *, path: str) -> list[ProjectIssue]:
    """Refuse `keep` as the default of the checks, and take it out of the document.

    Pydantic would word this refusal its own way, so the document is checked first. It has
    whatever shape the YAML gave it: anything but a mapping is left to pydantic.

    Parameters
    ----------
    raw
        The parsed document. A refused ``checks.on_failure`` is removed from it in place, so
        pydantic does not report the same field a second time.
    path
        The file's path, for issues.

    Returns
    -------
    list of ProjectIssue
        One per place a ``checks.on_failure: keep`` stands.
    """
    places: list[tuple[str, object]] = [("checks", raw.get("checks"))]
    environments = raw.get("environments")
    if isinstance(environments, dict):
        for name, override in environments.items():
            if isinstance(override, dict):
                places.append((f"environments.{_loc_part(name)}.checks", override.get("checks")))
    issues: list[ProjectIssue] = []
    for where, section in places:
        if isinstance(section, dict) and section.get("on_failure") == "keep":
            del section["on_failure"]
            issues.append(ProjectIssue(path, None, f"{where}.on_failure: {_KEEP_AS_DEFAULT}"))
    return issues


def parse_project_config(
    text: str, *, limits: DeployLimits | None = None, path: str = "lakana.yaml"
) -> ProjectConfig:
    """Parse and validate a `lakana.yaml`.

    Parameters
    ----------
    text
        The file's content.
    limits
        Supplies the YAML size limit; the defaults when `None`.
    path
        The file's repository-relative path, for issues.

    Returns
    -------
    ProjectConfig
        The validated configuration.

    Raises
    ------
    ProjectValidationError
        With every problem found.
    """
    limits = limits or DeployLimits()
    try:
        raw = load_yaml(normalize_source(text), what=path, max_bytes=limits.yaml_bytes)
    except YamlError as exc:
        raise ProjectValidationError([ProjectIssue(path, exc.line, str(exc))]) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProjectValidationError([ProjectIssue(path, 1, f"{path} must be a mapping.")])
    issues = _refuse_keep_as_default(raw, path=path)
    source = normalize_source(text)
    issues += _unknown_job_issues(raw, text=source, path=path)
    try:
        config = ProjectConfig.model_validate(raw)
    except pydantic.ValidationError as exc:
        issues += pydantic_issues(exc, path=path, line=None, text=source)
    else:
        issues += _schedule_without_cron_issues(config, text=source, path=path)
        issues += _notify_without_webhook_issues(config, text=source, path=path)
    if issues:
        raise ProjectValidationError(issues)
    return config
