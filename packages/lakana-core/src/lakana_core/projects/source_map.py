"""The source map: each deployed asset file, its text and the gate set that guards it.

A deploy records, for every asset file of a project version, the file's path, the table it
defines, its text (stored once per content digest) and its gate set: every table name the file
uses (`gate_tables`) and whether it reads something no table grant covers (`gate_raw`). The read
side shows a file's text only to a reader who passes that gate, so the gate is worked out here,
once, by the deploy's own parse, and never at read time.

The writes run inside `DeployService.record_deploy`, after every `tables` lock: first the texts,
in digest order, then the files. A text insert that meets an existing row updates it to itself,
so the row stays locked until the deploy commits and `sweep_project_file_texts` (which skips
locked rows) can never delete a text a running deploy is about to reference.

This module imports nothing from `lakana_core.runs` or `lakana_core.catalog.service`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from lakana_core.db import sqlstate
from lakana_core.identifiers import shown
from lakana_core.models import JobVersion, ProjectFile, ProjectFileText
from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.parse import file_references
from lakana_core.projects.python_assets import PYTHON_SOURCE_PREFIX

logger = logging.getLogger(__name__)

GATE_VERSION: int = 1
"""The version of the gate extraction; raised whenever `source_map_records` changes it."""
SOURCE_FILE_MAX_BYTES: int = 1024 * 1024
"""The largest text the read side shows (the deploy's own limit for an asset file)."""
SOURCE_LINE: int = 1
"""The line a file defines its table on: one asset file defines one table."""
_ROWS_PER_STATEMENT = 500
"""Rows per insert statement, well below Postgres's limit of bound parameters."""


def text_digest(text: str) -> str:
    """Give the hex SHA-256 of a text's UTF-8 bytes.

    Used alike for a file's text, its path and its table name.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    str
        64 lower-case hex digits.
    """
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class ProjectFileRecord:
    """One asset file of a deploy, with the gate set its parse gave.

    Parameters
    ----------
    path
        Repository-relative POSIX path.
    table
        The dotted table name discovery decided from the path.
    text
        The file's text as discovery read it (BOM removed, line endings normalized).
    normalized_sql
        The parse's `normalized_sql` (for Python, the source digest), which must equal the
        version manifest's entry for the record to be written.
    gate_tables
        Every table name the file uses, the defined table included; sorted, without duplicates.
    gate_raw
        Whether the file reads a file path or uses a connection.
    """

    path: str
    table: str
    text: str
    normalized_sql: str
    gate_tables: tuple[str, ...]
    gate_raw: bool

    @property
    def sha256(self) -> str:
        """Give the text's digest.

        Returns
        -------
        str
            `text_digest` of `text`.
        """
        return text_digest(self.text)

    @property
    def bytes(self) -> int:
        """Give the size of the text's UTF-8 encoding.

        Returns
        -------
        int
            The number of bytes.
        """
        return len(self.text.encode())


def _gate(
    asset: ParsedAsset, resolved: Sequence[ResolvedReference]
) -> tuple[tuple[str, ...], bool]:
    """Work out an asset's gate set (`GATE_VERSION` 1).

    Parameters
    ----------
    asset
        The parsed asset.
    resolved
        Its resolved model references.

    Returns
    -------
    tuple
        The sorted table names (the defined table, the body's table references, the Python
        inputs and the reference targets) and `gate_raw`.
    """
    raw = file_references(asset)
    names = {
        asset.table,
        *(name for name in asset.references if name not in raw),
        *(table for _, table in asset.inputs),
        *(reference.target for reference in resolved),
    }
    return tuple(sorted(names)), bool(raw) or bool(asset.connections)


def source_map_records(
    discovered: Sequence[DiscoveredAsset],
    parsed: Sequence[ParsedAsset],
    resolved: Mapping[str, Sequence[ResolvedReference]],
) -> tuple[ProjectFileRecord, ...]:
    """Build the source map's records of a deploy from its own discovery and parse.

    Parameters
    ----------
    discovered
        The asset files discovery read.
    parsed
        The parsed assets; each is joined to the file of the same path and table, and one
        without such a file gets no record.
    resolved
        The resolved model references per table (`resolve_references`).

    Returns
    -------
    tuple of ProjectFileRecord
        One record per parsed asset with a file, in `parsed`'s order.
    """
    files = {(found.path, found.table): found for found in discovered}
    records: list[ProjectFileRecord] = []
    for asset in parsed:
        found = files.get((asset.path, asset.table))
        if found is None:
            continue
        gate_tables, gate_raw = _gate(asset, resolved.get(asset.table, ()))
        records.append(
            ProjectFileRecord(
                path=found.path,
                table=found.table,
                text=found.text,
                normalized_sql=asset.normalized_sql,
                gate_tables=gate_tables,
                gate_raw=gate_raw,
            )
        )
    return tuple(records)


def _manifest_entries(manifest: object) -> set[tuple[str, str, str]]:
    """Read the `(table, path, normalized_sql)` of every asset a manifest holds.

    Parameters
    ----------
    manifest
        A version's `manifest_json`; untrusted, so anything of another shape gives nothing.

    Returns
    -------
    set of tuple
        The entries whose three fields are strings.
    """
    assets = manifest.get("assets") if isinstance(manifest, Mapping) else None
    if not isinstance(assets, list):
        return set()
    entries: set[tuple[str, str, str]] = set()
    for entry in assets:
        fields = (
            tuple(entry.get(key) for key in ("table", "path", "normalized_sql"))
            if isinstance(entry, Mapping)
            else ()
        )
        if len(fields) == 3 and all(isinstance(value, str) for value in fields):
            entries.add((fields[0], fields[1], fields[2]))
    return entries


def _chunks(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Cut rows into statements of at most `_ROWS_PER_STATEMENT`.

    Parameters
    ----------
    rows
        The rows, in lock order.

    Yields
    ------
    list of dict
        The next statement's rows.
    """
    for start in range(0, len(rows), _ROWS_PER_STATEMENT):
        yield rows[start : start + _ROWS_PER_STATEMENT]


def _matches(record: ProjectFileRecord, entries: set[tuple[str, str, str]]) -> bool:
    """Tell whether a record is the code of one of the manifest's entries.

    Parameters
    ----------
    record
        The deploy's record.
    entries
        The manifest's `(table, path, normalized_sql)` entries.

    Returns
    -------
    bool
        True when the entry exists and, for a Python asset (whose `normalized_sql` is the
        digest of its source), the record's text has that digest.
    """
    if (record.table, record.path, record.normalized_sql) not in entries:
        return False
    python = record.normalized_sql.startswith(PYTHON_SOURCE_PREFIX)
    return not python or record.normalized_sql == PYTHON_SOURCE_PREFIX + record.sha256


def write_source_map(
    session: Session, version: JobVersion, files: Sequence[ProjectFileRecord]
) -> int:
    """Write a version's source map, in the deploy's transaction; nothing is committed here.

    A record is written only when its table, path and `normalized_sql` equal an entry of the
    version's manifest and, for a Python asset, its text is the one that entry's digest names,
    so a reused version never takes a text that differs from the code it runs; any other record
    is skipped and its path logged. The texts are upserted in digest
    order, each conflict updating the row to itself, which locks it until the commit. A file row
    that exists is replaced only when its `gate_version` is older than `GATE_VERSION`.

    Parameters
    ----------
    session
        The deploy's session.
    version
        The new or reused project version.
    files
        The deploy's records (`source_map_records`).

    Returns
    -------
    int
        The number of file rows inserted or replaced.
    """
    entries = _manifest_entries(version.manifest_json)
    kept: dict[str, ProjectFileRecord] = {}
    for record in files:
        if _matches(record, entries):
            kept[text_digest(record.path)] = record
        else:
            logger.warning(
                "source map: %s does not match the deployed manifest; its file is not recorded",
                shown(record.path),
            )
    if not kept:
        return 0
    insert = postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    texts = {record.sha256: record.text for record in kept.values()}
    for chunk in _chunks([{"sha256": sha, "text": texts[sha]} for sha in sorted(texts)]):
        statement = insert(ProjectFileText).values(chunk)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["sha256"], set_={"sha256": statement.excluded.sha256}
            )
        )
    rows = [
        {
            "job_version_id": version.id,
            "path_sha256": path_sha256,
            "path": record.path,
            "table_sha256": text_digest(record.table),
            "table_name": record.table,
            "sha256": record.sha256,
            "line": SOURCE_LINE,
            "bytes": record.bytes,
            "gate_tables": list(record.gate_tables),
            "gate_raw": record.gate_raw,
            "gate_version": GATE_VERSION,
        }
        for path_sha256, record in sorted(kept.items())
    ]
    written = 0
    for chunk in _chunks(rows):
        statement = insert(ProjectFile).values(chunk)
        replaced = {
            column: statement.excluded[column]
            for column in chunk[0]
            if column not in ("job_version_id", "path_sha256")
        }
        result = session.execute(
            statement.on_conflict_do_update(
                index_elements=["job_version_id", "path_sha256"],
                set_=replaced,
                where=ProjectFile.__table__.c.gate_version < statement.excluded.gate_version,
            )
        )
        written += cast("CursorResult[Any]", result).rowcount
    return written


def _unreferenced(session: Session, batch: int, refused: set[str]) -> list[str]:
    """Select and lock the next batch of texts no project file references.

    Parameters
    ----------
    session
        The sweep's session.
    batch
        The most texts to take.
    refused
        Texts whose delete the foreign key refused in this sweep; never taken again.

    Returns
    -------
    list of str
        The digests, in order, each row locked (``FOR UPDATE SKIP LOCKED`` on Postgres).
    """
    query = select(ProjectFileText.sha256).where(
        ~exists().where(ProjectFile.sha256 == ProjectFileText.sha256)
    )
    if refused:
        query = query.where(ProjectFileText.sha256.not_in(refused))
    return list(
        session.scalars(
            query.order_by(ProjectFileText.sha256)
            .limit(batch)
            .with_for_update(skip_locked=True, of=ProjectFileText)
        )
    )


def _named_meanwhile(error: DBAPIError) -> bool:
    """Tell whether a delete failed because a file names the text (the foreign key).

    Parameters
    ----------
    error
        The error of the delete.

    Returns
    -------
    bool
        True for Postgres's SQLSTATE ``23503`` (whatever class the driver maps it to) and for
        SQLite's foreign key ``IntegrityError``, which carries no SQLSTATE.
    """
    state = sqlstate(error.orig)
    return state == "23503" or (state is None and isinstance(error, IntegrityError))


def _deleted(session: Session, shas: list[str]) -> bool:
    """Delete texts in a savepoint, unless the foreign key refuses it.

    Parameters
    ----------
    session
        The sweep's session.
    shas
        The texts to delete.

    Returns
    -------
    bool
        True when they were deleted; False when a file names one of them (nothing deleted).

    Raises
    ------
    DBAPIError
        For any other database error.
    """
    try:
        with session.begin_nested():
            session.execute(delete(ProjectFileText).where(ProjectFileText.sha256.in_(shas)))
    except DBAPIError as exc:
        if not _named_meanwhile(exc):
            raise
        return False
    return True


def sweep_project_file_texts(
    session: Session, *, batch: int = 500, should_stop: Callable[[], bool] = lambda: False
) -> int:
    """Delete the texts no project file references, in batches, each committed.

    Each batch takes its rows with ``FOR UPDATE SKIP LOCKED``, so a text a running deploy has
    upserted (and so locked) is skipped. A file committed between the select's snapshot and its
    lock can still name a text; the ``RESTRICT`` foreign key then refuses the batch's delete
    (SQLite's ``IntegrityError``, Postgres's SQLSTATE ``23503``, which pg8000 reports as a
    ``ProgrammingError``). The batch is then deleted text by text, each in a savepoint, so the
    named text stays, is not taken again in this sweep, and the others still go. Any other
    database error is raised.

    Parameters
    ----------
    session
        A session of the caller's own; each batch is committed.
    batch
        The most texts one batch deletes.
    should_stop
        Asked after each batch; true ends the sweep there.

    Returns
    -------
    int
        The number of texts deleted.
    """
    deleted = 0
    refused: set[str] = set()
    while True:
        shas = _unreferenced(session, batch, refused)
        if not shas:
            session.commit()
            return deleted
        if _deleted(session, shas):
            deleted += len(shas)
        else:
            for sha in shas:
                if _deleted(session, [sha]):
                    deleted += 1
                else:
                    refused.add(sha)
        session.commit()
        if should_stop():
            return deleted
