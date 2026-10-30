"""Validation findings with a file and line, collected rather than raised one at a time."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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


def withheld_lines(issues: Sequence[ProjectIssue]) -> list[dict[str, Any]]:
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
        and the objects it tells of. Stored beside the error (`DeployRun.error_withheld_json`),
        read by `error_for_reader`.
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
    same. A line that is no longer the one stored is replaced all the same. When nothing is left,
    the reader gets `WITHHELD_DEPLOY_MESSAGE`.

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
    for entry in withheld:
        if not isinstance(entry, dict):
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
    shown = [line for line in lines if line is not None]
    return "\n".join(shown) if shown else WITHHELD_DEPLOY_MESSAGE
