"""One logging set-up for the two long-running processes, `lakana-server` and `lakana-worker`."""

from __future__ import annotations

import copy
import logging
import re

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""Time, level and logger name before the message; the server and the worker print the same."""

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
"""The levels `LAKANA_LOG__LEVEL` accepts, in upper case."""

LAKANA_LOGGERS = ("lakana_core", "lakana_server", "lakana_worker", "lakana")
"""Lakana's own top-level loggers: the packages' names, which every module logger sits under."""

LEVEL_FOLLOWING_LOGGERS = (*LAKANA_LOGGERS, "alembic")
"""The loggers `configure_logging` sets to the level: Lakana's own and alembic's, whose
`alembic.runtime.migration` prints each "Running upgrade A -> B" step of a migration."""


CONTINUATION_PREFIX = "    | "
"""What every line after the first of a record starts with (see `SafeFormatter`)."""

UNSAFE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
"""The characters `escape_record` writes as escapes: those the CLI's `safe_text` escapes (C0
except the line feed and the tab, DEL, C1, the bidirectional controls, the line and paragraph
separators), and lone surrogates, which a strict stream cannot encode."""

MAX_RECORD_CHARS = 1_048_576
"""The most characters of a formatted record that are kept (1 MiB of text)."""

_TAB_SPACES = "    "
"""What a tab becomes, as in the CLI."""


def _escaped(match: re.Match[str]) -> str:
    r"""Spell one unsafe character the way the CLI's `safe_text` does.

    Parameters
    ----------
    match
        The character.

    Returns
    -------
    str
        `\x1b` for a code point below 256, `\u202e` above.
    """
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def escape_record(text: str) -> str:
    r"""Make the text of one formatted record safe for a log file or a terminal.

    Control characters are written as escapes (`\x1b`, `\x0d`, `\u202e`; the form of the CLI's
    `safe_text`). A backslash is **not** doubled: text that is already escaped, the output of
    `lakana_core.identifiers.shown` or of `%a`, holds no control character and passes as it is,
    so nothing is escaped twice. The line feed is the one control character kept, because a
    traceback is lines; every line after the first then starts with `CONTINUATION_PREFIX`.
    A line feed in a name, in an exception's text or in a library's message therefore cannot
    start a line that looks like a record of its own (a line that begins with a time, a level
    and a logger name): such a line is shown indented behind ``|``, and a reader who sees that
    prefix knows the line belongs to the record above. A carriage return, a vertical tab, a
    form feed, NEL and the Unicode separators are escaped, not treated as line breaks, so there
    is only one kind of line break to account for.

    Parameters
    ----------
    text
        The final text of a record: message, exception text and traceback.

    Returns
    -------
    str
        `text` with control characters escaped and continuation lines prefixed.
    """
    left_out = len(text) - MAX_RECORD_CHARS
    if left_out > 0:
        text = text[:MAX_RECORD_CHARS]
    safe = UNSAFE.sub(_escaped, text.replace("\t", _TAB_SPACES))
    if left_out > 0:
        safe += f"… ({left_out} more characters left out)"
    return safe.replace("\n", "\n" + CONTINUATION_PREFIX)


class SafeFormatterMixin:
    """Escape control characters in whatever the formatter it is mixed into produces.

    Put it before a `logging.Formatter` (or a subclass such as uvicorn's) in the bases. The
    escaping is applied to the *final* text, after the message, its arguments, the exception
    text, the traceback and the stack are joined, so it reaches what a call site never sees: an
    exception's `str`, a library's message. A call site still passes a user-chosen name through
    `shown`: this formatter exists only where `configure_logging` (or the server's uvicorn
    configuration) installed it, and an embedding process keeps its own handlers.

    It never raises: a formatter that raises loses the record. If the record cannot be
    formatted (arguments that do not fit the message, an object whose `str` fails), one line
    still says that a record from that logger and level could not be formatted, with the
    message template escaped.

    A record with a code (`extra=lakana_core.log_events.log_event(...)`, the attribute `event`)
    has ` event=<code>` at the end of its message's first line, before any traceback.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a record, with its code if it has one, then escape it.

        Parameters
        ----------
        record
            The record.

        Returns
        -------
        str
            The escaped text; a one-line notice when the record cannot be formatted.
        """
        try:  # a formatter that raises loses the record
            code = getattr(record, "event", None)
            if isinstance(code, str):
                # A copy: other handlers of the same record keep the message as it was.
                # On the first line, so a shipper that reads line by line sees it: a message can
                # hold line breaks (a driver error's "Background on this error" line).
                record = copy.copy(record)
                first, newline, rest = record.getMessage().partition("\n")
                record.msg, record.args = f"{first} event={code}{newline}{rest}", None
            return escape_record(super().format(record))  # ty: ignore[missing-attribute]
        except Exception:
            return self._fallback(record)

    def _fallback(self, record: logging.LogRecord) -> str:
        """Write a record that could not be formatted as one line plus its exception.

        Each part is made in its own guard, so that one that fails does not lose the others:
        the header of the formatter (made from a stand-in record with a fixed message, so it
        is a time, a level and a logger for the root format, and whatever uvicorn's formats
        start with), the place of the call, the message template, the exception.

        Parameters
        ----------
        record
            The record that could not be formatted.

        Returns
        -------
        str
            The escaped text; a constant string when even this fails.
        """
        try:
            parts: list[str] = []
            try:
                stand_in = copy.copy(record)
                stand_in.msg, stand_in.args = "(this record could not be formatted)", ()
                stand_in.exc_info = stand_in.exc_text = stand_in.stack_info = None
                parts.append(super().format(stand_in))  # ty: ignore[missing-attribute]
            except Exception:
                parts.append(
                    f"{record.levelname} {record.name}: (this record could not be formatted)"
                )
            try:
                parts.append(f"(at {record.pathname}:{record.lineno}")
                parts.append(f"; its message was {ascii(record.msg)[:200]})")
            except Exception:
                parts.append("; its message cannot be shown)")
            text = " ".join(parts)
            try:
                if record.exc_info and record.exc_info[0] is not None:
                    text += "\n" + self.formatException(record.exc_info)  # ty: ignore[missing-attribute]
            except Exception:
                text += "\n(its exception cannot be shown)"
            return escape_record(text)
        except Exception:
            return "(a log record could not be formatted)"


class SafeFormatter(SafeFormatterMixin, logging.Formatter):
    """The formatter of `configure_logging`: `logging.Formatter` with escaped output."""


def configure_logging(level: str = "INFO") -> None:
    """Print a process's log lines to stderr, once, at start-up, from `level` up.

    Without this the root logger has no handler and shows WARNING and above only, without a
    time, so every INFO line (the start-up notices, the sweep's summary, a denial that no audit
    row records) would be lost. Lakana's own loggers (`LAKANA_LOGGERS`) are set to `level`
    explicitly; the root logger, which every library's logger inherits, stays at WARNING, or at
    ERROR when `level` is ERROR. So a library's INFO and DEBUG lines are never printed, and
    DEBUG shows Lakana's lines and not those of an HTTP client or a SQL driver.

    The one deliberate exception is `alembic`: it follows `level` like Lakana's loggers, so the
    "Running upgrade A -> B" lines of `lakana-server migrate` (and of the worker's and the
    Compose bundle's migrate step) are printed at INFO and DEBUG. An operator watching an
    upgrade wants to see each step, and alembic logs nothing else of interest at INFO.

    The handler's formatter is `SafeFormatter`: control characters in any record, a library's
    and an exception's text included, are written as escapes, and a line feed starts a line
    marked with `CONTINUATION_PREFIX`, so text from outside cannot forge a log line.

    Nothing is added where the root logger already has a handler (an embedding process or a
    test has configured logging); no level is set then, on the root or on a Lakana logger.
    Importing a package never calls this: only an entry point does. The loggers of uvicorn do
    not propagate to the root logger, so a server that calls this prints no line twice; the
    server passes the same level to uvicorn itself.

    Parameters
    ----------
    level
        One of `LOG_LEVELS`, in any letter case; `LAKANA_LOG__LEVEL` has been validated
        already when it comes from the settings.

    Raises
    ------
    ValueError
        If `level` is not one of `LOG_LEVELS`.
    """
    name = level.upper()
    if name not in LOG_LEVELS:
        raise ValueError(f"The log level must be one of {', '.join(LOG_LEVELS[:-1])} or ERROR.")
    if logging.getLogger().handlers:
        return
    number = logging.getLevelNamesMapping()[name]
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter(LOG_FORMAT))
    logging.basicConfig(level=max(number, logging.WARNING), handlers=[handler])
    for logger_name in LEVEL_FOLLOWING_LOGGERS:
        logging.getLogger(logger_name).setLevel(number)
