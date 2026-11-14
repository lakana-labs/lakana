"""A log line with a code ends its message with `event=<code>`, and the record carries it."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from lakana_core.log_events import WHERE, LogEvent, log_event
from lakana_core.log_setup import LOG_FORMAT, SafeFormatter

_Captured = tuple[logging.Logger, io.StringIO, list[logging.LogRecord]]


class _Keep(logging.Handler):
    """Keep every record it is given.

    Parameters
    ----------
    records
        Where to keep them.
    """

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record.

        Parameters
        ----------
        record
            The record.
        """
        self._records.append(record)


@pytest.fixture
def captured() -> Iterator[_Captured]:
    """Give a logger that writes through `SafeFormatter` into a stream and keeps its records.

    Yields
    ------
    tuple
        The logger, the stream and the kept records.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter(LOG_FORMAT))
    records: list[logging.LogRecord] = []
    logger = logging.Logger("lakana_test.events", logging.DEBUG)
    logger.addHandler(handler)
    logger.addHandler(_Keep(records))
    yield logger, stream, records


def test_the_line_ends_with_the_code_and_the_record_carries_it(captured: _Captured) -> None:
    logger, stream, records = captured

    logger.warning("Ended %d runs.", 2, extra=log_event(LogEvent.WORKER_LOST))

    assert (
        stream.getvalue()
        .rstrip("\n")
        .endswith("lakana_test.events: Ended 2 runs. event=worker_lost")
    )
    (record,) = records
    assert record.event == "worker_lost"  # ty: ignore[missing-attribute]
    assert record.getMessage() == "Ended 2 runs."


def test_the_code_stays_on_the_first_line_before_a_traceback(captured: _Captured) -> None:
    logger, stream, _ = captured

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception(
            "Polling iteration failed; continuing.", extra=log_event(LogEvent.POLL_FAILED)
        )

    first, *rest = stream.getvalue().split("\n")
    assert first.endswith("Polling iteration failed; continuing. event=poll_failed")
    assert any("ValueError: boom" in line for line in rest)


def test_a_line_without_a_code_is_unchanged(captured: _Captured) -> None:
    logger, stream, _ = captured

    logger.info("Nothing to alert on.")

    assert stream.getvalue().rstrip("\n").endswith(": Nothing to alert on.")


def test_every_code_is_lowercase_words_and_has_a_process() -> None:
    for code in LogEvent:
        assert code.value.replace("_", "").isalpha() and code.value == code.value.lower()
    assert set(WHERE) == set(LogEvent)
    assert set(WHERE.values()) <= {"server", "worker", "server, worker"}


def test_a_multi_line_message_has_the_code_on_its_first_line(captured: _Captured) -> None:
    """A driver error's text has a second line; the code must not land on it."""
    logger, stream, _ = captured

    logger.warning(
        "The worker's heartbeat is still failing (%d in a row): %s",
        2,
        "connection refused\n(Background on this error at: https://sqlalche.me/e/20/e3q8)",
        extra=log_event(LogEvent.DATABASE_UNREACHABLE),
    )

    first, second, *_ = stream.getvalue().split("\n")
    assert first.endswith("(2 in a row): connection refused event=database_unreachable")
    assert "event=" not in second and "Background on this error" in second


def test_an_exception_text_with_line_breaks_keeps_the_code_on_the_first_line(
    captured: _Captured,
) -> None:
    logger, stream, _ = captured

    try:
        raise RuntimeError("first\nsecond")
    except RuntimeError:
        logger.exception(
            "Scheduler step failed; continuing.", extra=log_event(LogEvent.SCHEDULER_STEP_FAILED)
        )

    lines = stream.getvalue().split("\n")
    assert lines[0].endswith("Scheduler step failed; continuing. event=scheduler_step_failed")
    assert sum("event=" in line for line in lines) == 1
