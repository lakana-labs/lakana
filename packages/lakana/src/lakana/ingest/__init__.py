"""Helpers that read a source through a connection; they run inside a Lakana task.

An asset names the connections it reads in ``@asset(connections=[...])``. Inside its task,
`lakana.ingest.connections.get` fetches one of them, with its secret, from the server with the
task's own token, once per attempt, and keeps it in memory only. Nothing here imports an
optional dependency when it is imported; each helper imports its own library.
"""

from __future__ import annotations

from lakana.ingest import connections, files
from lakana.ingest._errors import (
    CapExceeded,
    ConnectionChanged,
    ConnectionNotDeclared,
    ConnectionRefused,
    FileShapeError,
    IngestError,
    SourceError,
)

__all__ = [
    "CapExceeded",
    "ConnectionChanged",
    "ConnectionNotDeclared",
    "ConnectionRefused",
    "FileShapeError",
    "IngestError",
    "SourceError",
    "connections",
    "files",
]
