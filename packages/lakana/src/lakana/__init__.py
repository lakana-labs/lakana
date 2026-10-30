"""Lakana SDK: a typed Python client for the Lakana platform API."""

from __future__ import annotations

from lakana._version import resolve_version
from lakana.assets import asset, output_dir
from lakana.client import Client
from lakana.errors import (
    ArtifactCorrupt,
    AuthenticationError,
    ConfigError,
    Conflict,
    DeployFailed,
    InvalidResponse,
    JobStartsNotRecorded,
    LakanaConnectionError,
    LakanaError,
    NotFound,
    PayloadTooLarge,
    PermissionDenied,
    QueryFailed,
    RunFailed,
    ServerError,
    ValidationError,
)

__version__ = resolve_version()

__all__ = [
    "ArtifactCorrupt",
    "AuthenticationError",
    "Client",
    "ConfigError",
    "Conflict",
    "DeployFailed",
    "InvalidResponse",
    "JobStartsNotRecorded",
    "LakanaConnectionError",
    "LakanaError",
    "NotFound",
    "PayloadTooLarge",
    "PermissionDenied",
    "QueryFailed",
    "RunFailed",
    "ServerError",
    "ValidationError",
    "__version__",
    "asset",
    "output_dir",
]
