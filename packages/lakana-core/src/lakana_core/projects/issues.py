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
        missing table gets. Set exactly when `targets` is. Not compared.
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


def withheld_lines(issues: Sequence[ProjectIssue]) -> list[dict[str, Any]]:
    """List the lines of a stored error that a reader may be shown otherwise.

    Parameters
    ----------
    issues
        The issues the error lists.

    Returns
    -------
    list of dict
        One ``{"line", "withheld", "targets"}`` per issue that tells of a target: its rendered
        line, the line shown instead, and the tables it tells of. Stored beside the error
        (`DeployRun.error_withheld_json`), read by `error_for_reader`.
    """
    return [
        {
            "line": issue.render(),
            "withheld": issue.render_message(issue.withheld),
            "targets": list(issue.targets),
        }
        for issue in issues
        if issue.targets and issue.withheld is not None
    ]


def error_for_reader(message: str, withheld: object, may_read: Callable[[str], bool]) -> str:
    """Show a stored error to a reader who is not the one it was written for.

    Each line that tells of a table the reader may not read is replaced by the line a missing
    table gets, so a missing table and an unreadable one read the same. A replaced line that is
    already shown is not shown twice: one reference to a missing table makes one line.

    Parameters
    ----------
    message
        The stored error.
    withheld
        What `withheld_lines` stored beside it; anything else (an error written before the
        column existed) withholds nothing.
    may_read
        Whether the reader may read a table, by its dotted name.

    Returns
    -------
    str
        The error as the reader may see it.
    """
    if not isinstance(withheld, list):
        return message
    instead: dict[str, str] = {}
    for entry in withheld:
        if not isinstance(entry, dict):
            continue
        line, other, targets = entry.get("line"), entry.get("withheld"), entry.get("targets")
        if not (isinstance(line, str) and isinstance(other, str) and isinstance(targets, list)):
            continue
        if not all(isinstance(t, str) and may_read(t) for t in targets):
            instead[line] = other
    shown: list[str] = []
    for line in message.split("\n"):
        replaced = instead.get(line, line)
        if line in instead and replaced in shown:
            continue
        shown.append(replaced)
    return "\n".join(shown)
