"""A name a caller chose never forges a log record or colours a terminal.

The hostile name holds a line break followed by text that looks like a log record, and an
escape character. Each test drives a real log call and reads the record's message; the last
test walks the syntax trees of the product packages for a new call that would print a raw
name.
"""

from __future__ import annotations

import ast
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import ARTIFACT_REF, artifact_service, store_version
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.artifacts.sweep import sweep_artifacts
from lakana_core.auth import PrincipalRef
from lakana_core.identifiers import TableRef
from lakana_core.models import ArtifactVersion, Environment
from lakana_core.storage.fsspec_storage import FsspecStorage

HOSTILE = "x\n2026-10-08 ERROR forged record\x1b[2J"
"""A line break, then text that reads as a log record, then an escape sequence."""

ESCAPED = "x\\n2026-10-08 ERROR forged record\\x1b[2J"
"""What `shown` makes of it."""


def _assert_one_line_and_escaped(records: list[logging.LogRecord], expected: int) -> None:
    """Check that the records name the hostile name only in its escaped form.

    Parameters
    ----------
    records
        The captured records that name the hostile name.
    expected
        How many there should be.
    """
    assert len(records) == expected
    for record in records:
        message = record.getMessage()
        assert "\n" not in message and "\x1b" not in message
        assert ESCAPED in message


def test_a_failed_removal_of_bytes_logs_the_artifact_name_escaped(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a version and dropping an artifact: the names in `artifacts/service.py`."""
    ref = TableRef(ARTIFACT_REF.environment, ARTIFACT_REF.namespace, HOSTILE)
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ref, {"a": b"one"})
    store_version(service, principal, ref, {"a": b"two"})

    def broken(self: FsspecStorage, path: str) -> None:
        raise OSError("storage is down")

    monkeypatch.setattr(FsspecStorage, "delete", broken)
    with caplog.at_level(logging.WARNING):
        service.delete_version(principal, ref, 1)
        service.drop_artifact(principal, ref)
    named = [r for r in caplog.records if "forged" in r.getMessage()]
    _assert_one_line_and_escaped(named, 2)


def test_a_kept_version_logs_the_artifact_name_escaped(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """The line that says why a deleted version's files were kept."""
    ref = TableRef(ARTIFACT_REF.environment, ARTIFACT_REF.namespace, HOSTILE)
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ref, {"a": b"one"})
    store_version(service, principal, ref, {"a": b"two"})
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 2)
        .values(manifest_json={"files": "damaged"})
    )
    session.commit()
    with caplog.at_level(logging.WARNING):
        service.delete_version(principal, ref, 1)
    _assert_one_line_and_escaped([r for r in caplog.records if "Deleting version" in r.msg], 1)


def test_the_sweep_logs_an_environment_name_escaped(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    session.add(
        Environment(
            id=uuid.uuid4(), name=HOSTILE, storage_root="file:///nowhere", storage_options={}
        )
    )
    session.commit()

    class Broken:
        """A resolver whose storage cannot be reached."""

        def for_environment(self, name: str) -> object:
            """Fail as an unreachable store does.

            Parameters
            ----------
            name
                The environment.

            Raises
            ------
            OSError
                Always.
            """
            raise OSError("unreachable")

    with caplog.at_level(logging.WARNING):
        sweep_artifacts(
            session,
            Broken(),  # ty: ignore[invalid-argument-type]
            now=datetime(2026, 10, 8, tzinfo=UTC),
            upload_ttl_seconds=3600,
        )
    _assert_one_line_and_escaped([r for r in caplog.records if "skipped environment" in r.msg], 1)


# ---- the guard ------------------------------------------------------------------------------

_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})
_LOGGERS = frozenset({"logger", "_logger", "log", "_log", "LOG", "_LOG"})
_RAW_CALLS = frozenset({"dotted"})
"""Methods whose result is a caller's name: `TableRef.dotted()`."""
_RAW_ATTRIBUTES = frozenset({"name"})
"""Attributes that hold a caller's name: `ref.name`, `principal.name`."""
_RAW_NAMES = frozenset({"asset"})
"""Variables that hold a caller's name wherever they are logged."""


def raw_names_in_log_calls(source: str) -> list[int]:
    """Find logging calls that print a caller's name without `shown`.

    Parameters
    ----------
    source
        The text of a Python module.

    Returns
    -------
    list of int
        The line of each logging call with an argument that is, or contains outside a call to
        `shown`, a call to `.dotted()`, an attribute `.name`, or a variable named in `_RAW_NAMES`.
    """
    found: list[int] = []

    def raw(node: ast.AST, wrapped: bool) -> bool:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "shown"
        ):
            wrapped = True
        if not wrapped:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_CALLS
            ):
                return True
            if isinstance(node, ast.Attribute) and node.attr in _RAW_ATTRIBUTES:
                return True
            if isinstance(node, ast.Name) and node.id in _RAW_NAMES:
                return True
        return any(raw(child, wrapped) for child in ast.iter_child_nodes(node))

    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LEVELS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _LOGGERS
        ):
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(raw(argument, False) for argument in arguments):
                found.append(node.lineno)
    return found


def test_the_guard_finds_a_raw_name_and_accepts_a_shown_one() -> None:
    bad = (
        "logger.warning('a %s', ref.dotted())\n"
        "log.error('b %s', f(principal.name))\n"
        "_log.info('c %s', asset)\n"
        "logger.exception('d %s %s', 1, row.name)\n"
        "logger.warning(f'a {ref.dotted()}')\n"
        "logger.warning('a %s' % ref.dotted())\n"
        "logger.warning('a {}'.format(row.name))\n"
        "logger.warning('a ' + row.name)\n"
        "logger.warning(msg=f'{row.name}')\n"
        "logger.warning('a', extra={'n': row.name})\n"
    )
    assert raw_names_in_log_calls(bad) == list(range(1, 11))
    good = (
        "logger.warning('a %s', shown(ref.dotted()))\n"
        "log.error('b %s', f(shown(principal.name)))\n"
        "logger.info('c %s', type(exc).__name__)\n"
        "logger.warning('d %s', upload.uri)\n"
        "other.warning('e %s', ref.dotted())\n"
        "logger.warning(f'a {shown(ref.dotted())}')\n"
    )
    assert raw_names_in_log_calls(good) == []


@pytest.mark.slow
def test_no_product_log_call_prints_a_raw_name() -> None:
    packages = Path(__file__).resolve().parents[2]
    offenders = [
        f"{path.relative_to(packages)}:{line}"
        for path in sorted(packages.glob("*/src/**/*.py"))
        for line in raw_names_in_log_calls(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "a log call prints a name a caller chose without `shown(...)` "
        "(lakana_core.identifiers.shown): " + ", ".join(offenders)
    )
