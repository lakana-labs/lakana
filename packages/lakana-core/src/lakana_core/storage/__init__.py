"""Storage abstraction over local and object storage backends."""

from lakana_core.storage.base import ChildEntry, Storage, StreamWriter
from lakana_core.storage.fsspec_storage import FsspecStorage, build_file_io, build_storage
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver

__all__ = [
    "ChildEntry",
    "FsspecStorage",
    "ResolvedStorage",
    "Storage",
    "StorageResolver",
    "StreamWriter",
    "build_file_io",
    "build_storage",
]
