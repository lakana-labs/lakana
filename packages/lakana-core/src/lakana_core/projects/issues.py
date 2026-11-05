"""Validation findings with a file and line, collected rather than raised one at a time."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from lakana_core.errors import ValidationError


@dataclass(frozen=True)
class ProjectIssue:
    """One problem found in a project.

    Parameters
    ----------
    path
        Repository-relative POSIX path of the file, e.g. ``assets/bronze/x.sql``.
    line
        1-based line, or `None` when the problem has no single line.
    message
        What is wrong, phrased for the person who wrote the file.
    targets
        The tables the message tells something of (that one exists, its kind, its key or its
        columns, or what a principal may read of it); empty for most issues. Not compared.
    withheld
        The message shown instead to a reader who may not read one of `targets`: the one a
        missing table gets, or an empty text to show nothing in its place (a missing table makes
        one line where a found one can make several). Set exactly when `targets` is. Not
        compared.
    """

    path: str
    line: int | None
    message: str
    targets: tuple[str, ...] = field(default=(), compare=False)
    withheld: str | None = field(default=None, compare=False)

    def render(self) -> str:
        """Format as ``path:line: message`` (or ``path: message``).

        Returns
        -------
        str
            The one-line form shown in `error_message` and the CLI.
        """
        return self.render_message(self.message)

    def render_message(self, message: str) -> str:
        """Put the file and line in front of a message.

        Parameters
        ----------
        message
            The message.

        Returns
        -------
        str
            ``path:line: message`` (or ``path: message``).
        """
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: {message}"


class ProjectValidationError(ValidationError):
    """Every issue found in a project, raised together.

    Parameters
    ----------
    issues
        The findings, in the order found. Must not be empty.
    """

    def __init__(self, issues: Sequence[ProjectIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(issue.render() for issue in self.issues))


WITHHELD_DEPLOY_MESSAGE = (
    "This deploy failed because of objects you may not read, so its error is not shown to you. "
    "The principal that started the deploy sees it whole, and so does anyone who may read those "
    "objects."
)
"""What a reader gets when every line of a deploy's error tells of an object it may not read."""


MAX_LISTED_ISSUES = 50
"""Most project issues a stored deploy error lists to one reader; the rest are counted."""


def _dropped(issue: ProjectIssue) -> bool:
    """Say whether a reader who may not read an issue's targets is shown no line for it.

    Parameters
    ----------
    issue
        The issue.

    Returns
    -------
    bool
        True for the second and later problems of one target (`ProjectIssue.withheld` empty).
    """
    return bool(issue.targets) and issue.withheld == ""


def stored_error(issues: Sequence[ProjectIssue]) -> tuple[str, list[dict[str, Any]]]:
    """Render a failure's issues for storing, so each reader's view is capped as it reads it.

    The cap is applied per reader, after withholding (`error_for_reader`): capped once, an
    unreadable target's later lines, left out for a reader who may not read it, would make that
    reader's list shorter and its count larger than a missing target's (#442). Every reader is
    shown a line for each issue `_dropped` is false for, so the first `MAX_LISTED_ISSUES` of those
    bound what any reader is listed: the text stops there, and the issues after it are kept only
    as a count and, for the dropped ones, their targets.

    Parameters
    ----------
    issues
        The issues, in their order.

    Returns
    -------
    tuple
        The text to store as the error, and what to store beside it (`withheld_lines`).
    """
    if len(issues) <= MAX_LISTED_ISSUES:
        return "\n".join(issue.render() for issue in issues), _target_lines(issues)
    kept, shown = 0, 0
    while kept < len(issues) and shown < MAX_LISTED_ISSUES:
        shown += not _dropped(issues[kept])
        kept += 1
    listed, rest = issues[:kept], issues[kept:]
    counts: dict[tuple[str, ...], int] = {}
    for issue in rest:
        if _dropped(issue):
            counts[issue.targets] = counts.get(issue.targets, 0) + 1
    starts, index = [], 0
    for issue in listed:
        starts.append(index)
        index += issue.render().count("\n") + 1
    summary = {
        "issues": starts,
        "more": sum(not _dropped(issue) for issue in rest),
        "more_withheld": [{"targets": list(t), "count": n} for t, n in counts.items()],
    }
    return "\n".join(issue.render() for issue in listed), [*_target_lines(listed), summary]


def withheld_lines(issues: Sequence[ProjectIssue]) -> list[dict[str, Any]]:
    """List what is stored beside a failure's error (`stored_error`).

    Parameters
    ----------
    issues
        The issues the error lists, in its order and from its first line.

    Returns
    -------
    list of dict
        `_target_lines`, and for more than `MAX_LISTED_ISSUES` issues one more entry
        ``{"issues", "more", "more_withheld"}``: the first line of each issue the text holds,
        how many issues after them every reader is shown, and the targets of the others with
        their counts. Stored beside the error (`DeployRun.error_withheld_json`), read by
        `error_for_reader`.
    """
    return stored_error(issues)[1]


def _target_lines(issues: Sequence[ProjectIssue]) -> list[dict[str, Any]]:
    """List the lines of a stored error that a reader may be shown otherwise.

    Parameters
    ----------
    issues
        The issues the error lists, in its order and from its first line.

    Returns
    -------
    list of dict
        One ``{"index", "line", "withheld", "targets"}`` per issue that tells of an object: the
        position of its line in the error, the line, the line shown instead (empty for none),
        and the objects it tells of.
    """
    lines: list[dict[str, Any]] = []
    index = 0
    for issue in issues:
        rendered = issue.render()
        if issue.targets and issue.withheld is not None:
            lines.append(
                {
                    "index": index,
                    "line": rendered,
                    "withheld": issue.render_message(issue.withheld) if issue.withheld else "",
                    "targets": list(issue.targets),
                }
            )
        index += rendered.count("\n") + 1
    return lines


def error_for_reader(message: str, withheld: object, may_read: Callable[[str], bool]) -> str:
    """Show a stored error to a reader who is not the one it was written for.

    Each line that tells of an object the reader may not read is replaced, by its position and
    never by its text (two references can make the same line), with the line a missing object
    gets, or left out when that is empty (the second and later problems of one reference, an
    object whose absence makes no line), so a missing object and an unreadable one read the
    same. A line that is no longer the one stored is replaced all the same. An error of more than
    `MAX_LISTED_ISSUES` issues is capped after that (`stored_error`), so the lines listed and the
    count after them are a missing object's too. When nothing is left, the reader gets
    `WITHHELD_DEPLOY_MESSAGE`. The deployer reads through it too, with every object readable.

    Parameters
    ----------
    message
        The stored error.
    withheld
        What `withheld_lines` stored beside it; anything else (an error written before the
        column existed) withholds nothing.
    may_read
        Whether the reader may read an object, by its dotted name.

    Returns
    -------
    str
        The error as the reader may see it.
    """
    if not isinstance(withheld, list):
        return message
    lines: list[str | None] = list(message.split("\n"))
    summary: dict[str, Any] | None = None
    for entry in withheld:
        if not isinstance(entry, dict):
            continue
        if "issues" in entry:
            summary = entry
            continue
        index, other, targets = entry.get("index"), entry.get("withheld"), entry.get("targets")
        if not (
            type(index) is int
            and 0 <= index < len(lines)
            and isinstance(other, str)
            and isinstance(targets, list)
        ):
            continue
        if not all(isinstance(t, str) and may_read(t) for t in targets):
            lines[index] = other or None
    if summary is not None and _well_formed(summary):
        return _capped(lines, summary, may_read)
    shown = [line for line in lines if line is not None]
    return "\n".join(shown) if shown else WITHHELD_DEPLOY_MESSAGE


def _well_formed(summary: dict[str, Any]) -> bool:
    """Say whether a stored summary (`stored_error`) has the shape `_capped` reads.

    Parameters
    ----------
    summary
        The entry.

    Returns
    -------
    bool
        False for anything `stored_error` does not write; such an error is shown uncapped.
    """
    starts, more, others = summary.get("issues"), summary.get("more"), summary.get("more_withheld")
    return (
        isinstance(starts, list)
        and all(type(start) is int for start in starts)
        and type(more) is int
        and isinstance(others, list)
        and all(isinstance(other, dict) for other in others)
    )


def _capped(
    lines: list[str | None], summary: dict[str, Any], may_read: Callable[[str], bool]
) -> str:
    """List a reader's first `MAX_LISTED_ISSUES` issues and count the rest it is shown.

    Parameters
    ----------
    lines
        The stored error's lines, a withheld one replaced or `None` (`error_for_reader`).
    summary
        The entry `stored_error` stored beside the error.
    may_read
        Whether the reader may read an object, by its dotted name.

    Returns
    -------
    str
        The issues the reader is shown, at most `MAX_LISTED_ISSUES`, then ``…and N more issues``
        when there are more.
    """
    bounds = [*summary["issues"], len(lines)]
    issues = [[line for line in lines[a:b] if line is not None] for a, b in pairwise(bounds)]
    shown = [issue for issue in issues if issue]
    more = summary["more"] + max(len(shown) - MAX_LISTED_ISSUES, 0)
    for entry in summary["more_withheld"]:
        targets, count = entry.get("targets"), entry.get("count")
        if (
            type(count) is int
            and isinstance(targets, list)
            and all(isinstance(t, str) and may_read(t) for t in targets)
        ):
            more += count
    text = "\n".join(line for issue in shown[:MAX_LISTED_ISSUES] for line in issue)
    if more:
        text = f"{text}\n…and {more} more issues"
    return text or WITHHELD_DEPLOY_MESSAGE
