"""Resolution of an environment to usable storage."""

from __future__ import annotations

import posixpath
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pyiceberg.io import FileIO
from pyiceberg.table import TableProperties
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.config import StorageConfig
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import (
    NamespacePath,
    refuse_percent_in_new_name,
    validate_path_component,
)
from lakana_core.models import Environment, Secret
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUse, require_use
from lakana_core.storage.base import Storage
from lakana_core.storage.fsspec_storage import (
    FSSPEC_CHAIN,
    ROOT_CHAIN_MESSAGE,
    build_file_io,
    build_storage,
)

MANAGED_PREFIX = "managed"
"""Zone holding Iceberg tables, as opposed to the raw landing zone."""

ARTIFACTS_PREFIX = "artifacts"
"""Directory of the storage root that holds artifacts; no table may live in it."""

RAW_PREFIX = "raw"
"""Zone an `EXTERNAL` table's `location_pattern` must be confined to."""

_FORBIDDEN_TABLE_PROPERTIES = frozenset({TableProperties.WRITE_PY_LOCATION_PROVIDER_IMPL})
"""Table properties rejected outright, whatever their value.

`write.py-location-provider.impl` names a dotted ``module.ClassName``
which pyiceberg's `load_location_provider` passes to
`importlib.import_module` and then instantiates
(`pyiceberg.table.locations._import_location_provider`). That is an
arbitrary-import primitive driven by caller input, and no path check can
undo it: the import and the instantiation have already happened by the
time a path exists to confine.
"""

_WRITE_OBJECT_STORAGE_PATH = "write.object-storage.path"
"""The Apache Iceberg table-properties spec's write-object-storage-path key.

Hardcoded rather than pinned to a `pyiceberg.table.TableProperties`
constant: the installed pyiceberg (0.12) has no such constant, and its
`ObjectStoreLocationProvider` reads only `write.data.path`,
`write.object-storage.enabled`, and `write.object-storage.partitioned-paths`
— it does not currently honour this key at all. It is refused defensively
anyway: a future pyiceberg
release that does honour it would otherwise reopen exactly the escape
`write.data.path` already closes, and there is no cost to refusing a key
nothing reads yet.
"""

_PATH_TABLE_PROPERTIES = frozenset(
    {
        TableProperties.WRITE_METADATA_PATH,
        TableProperties.WRITE_DATA_PATH,
        _WRITE_OBJECT_STORAGE_PATH,
    }
)
"""Table properties whose value is a location, and so are refused outright.

`write.metadata.path` and `write.data.path` are read by
`pyiceberg.table.locations.LocationProvider.__init__`; `write.metadata.path`
takes effect immediately, redirecting the metadata file `create_table`
writes, and `write.data.path` is persisted verbatim for whichever component
writes data next to honour. `write.object-storage.path` is not currently
read by the installed pyiceberg at all (see its own docstring above).

Originally these were merely confined to the storage root, the
same as a caller-supplied `location=`. Confining them independently of
`location=` was not enough: a managed table's location is Lakana's decision
alone, made once at `create_table` and never revisited, so an in-root value
for one of these properties could still redirect a table's metadata or data
to an arbitrary in-root directory — including one colliding with another
table's own location — the same way a caller-supplied `location=` could
before that was refused outright too. They are now refused whatever their value, exactly as
`location=` is.

The keys come from `pyiceberg.table.TableProperties` where a constant
exists, so a renamed **constant** fails at import rather than silently
ceasing to match. A renamed **value** would not — the guard would simply
stop matching — so `test_the_guarded_property_keys_are_the_ones_pyiceberg_reads`
pins the resolved strings.
"""


_GLOB_METACHARACTERS = frozenset("*?[]")
"""Characters `fnmatch`/DuckDB's `read_parquet` treat as glob wildcards.

A path containing one of these is never a literal file location as far as
`is_within_location` is concerned: DuckDB expands a glob in a local path itself, so
``.../managed/*/*/data/*.parquet`` — a string that legitimately starts with
a broad table's own location, `posixpath.normpath`-canonical, containing no
`.`/`..`/doubled slash — would still let a single "file path" match every
table's data across the whole `managed/` zone once DuckDB opened it.
"""


SCHEME_ALIASES = {"s3a": "s3", "s3n": "s3", "abfss": "abfs"}
"""Schemes that address the same storage as another one, by their usual name."""

PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
"""A percent escape, which a backend may decode: a spelling with one cannot be normalised exactly.

A glob character is taken literally when locations are compared: a storage backend deletes and
lists by literal names, so it cannot make two spellings name one directory."""


def normalised_path(rest: str) -> str | None:
    """Spell the path of a location, the part after ``scheme://``, the one way it is compared.

    The single definition of a canonical path: empty segments (a doubled slash anywhere, a
    leading one included, which `posixpath.normpath` keeps) and ``.`` segments are dropped, one
    leading ``/`` is kept, and no trailing ``/``. A path is canonical when it comes back
    unchanged.

    Parameters
    ----------
    rest
        The path, such as ``/data/lake`` or ``bucket/lake``.

    Returns
    -------
    str or None
        The path; `None` when it has a ``..`` segment, whose target cannot be told from the
        spelling alone.
    """
    segments = rest.split("/")
    if ".." in segments:
        return None
    kept = "/".join(segment for segment in segments if segment not in {"", "."})
    return ("/" if rest.startswith("/") else "") + kept


def _is_canonical_uri(uri: str) -> bool:
    """Check whether `uri` has a scheme, no glob metacharacters, and a canonical path.

    Parameters
    ----------
    uri
        An absolute URI to check.

    Returns
    -------
    bool
        `True` if `uri` has a ``scheme://`` prefix, its path component is not empty (and, for
        ``file``, absolute with an empty host), contains none of `_GLOB_METACHARACTERS`, and
        comes back unchanged from `normalised_path` (no `.`, `..`, doubled slash, leading
        doubled slash or trailing slash).
    """
    scheme, sep, rest = uri.partition("://")
    if not sep or not rest:
        return False
    if scheme.lower() == "file" and not rest.startswith("/"):
        return False  # relative, or with a host: no one reading
    if any(char in rest for char in _GLOB_METACHARACTERS):
        return False
    return normalised_path(rest) == rest


ROOT_UNJUDGEABLE_MESSAGE = (
    "The storage root {root!r} cannot be compared with other locations. Write it without .. "
    "segments, percent escapes (such as %20) and the characters * ? [ ], as a plain "
    "scheme://path."
)
ROOT_RELATIVE_MESSAGE = (
    "The storage root {root!r} is not an absolute path on this machine: a file root is "
    "file:// followed by an absolute path and no host, such as 'file:///data/lake'. A relative "
    "path would be read against each process's working directory."
)
ROOT_DISK_MESSAGE = (
    "The storage root {root!r} is the root of the disk, which cannot hold an environment: "
    "Lakana's storage would read and write its files in each process's working directory. Use "
    "a directory instead, such as 'file:///data/lakana'."
)
ROOT_SPELLING_MESSAGE = (
    "The storage root {root!r} is written differently from the way Lakana compares locations, "
    "so the files of one environment could be taken for another's. Use {canonical!r} instead."
)
ROOT_BUCKET_MESSAGE = (
    "The storage root {root!r} names no bucket or container: an object-store root starts with "
    "one, such as {example!r}. Without it the storage would take the first directory, or "
    "managed/ itself, for the bucket, while Lakana compares locations as written."
)
_BUCKET_SCHEMES = frozenset({"s3", "abfs"})
"""Schemes whose root starts with a bucket or container (`ROOT_BUCKET_MESSAGE`)."""
ROOT_SCHEME_MESSAGE = (
    "The storage root {root!r} is in the scheme {root_scheme!r}, but the environment's storage "
    "scheme is {scheme!r}: a root reaches the storage of its own scheme, so the environment's "
    "files would land somewhere other than its storage ('local://' is each process's working "
    "directory, 'memory://' its memory). Write the root in the {scheme!r} scheme, such as "
    "{example!r}, or give the environment the storage scheme of its root if Lakana supports it "
    "('file', 's3' or 'abfs')."
)

ROOT_SCHEME_EXAMPLES = {
    "file": "file:///data/lakana",
    "s3": "s3://bucket/lakana",
    "abfs": "abfs://container/lakana",
}
"""A root to suggest for each scheme `StorageConfig.scheme` can take."""


def check_root_scheme(root: str, scheme: str) -> None:
    """Refuse a storage root whose scheme is not the environment's storage scheme.

    A root reaches the storage of its own scheme whatever the environment's storage scheme says:
    fsspec reads ``local://``, ``local:///`` and ``local:`` as the working directory and
    ``memory://`` as the process's memory, and a ``file`` root on an ``s3`` environment would
    use the local disk. The root's scheme is compared after `SCHEME_ALIASES` and case, so
    ``s3a://`` is an ``s3`` root (and `check_storage_root` then names its spelling). A root
    without ``scheme://`` has no scheme to compare; `check_storage_root` refuses it.

    Parameters
    ----------
    root
        The root, ``scheme://path``.
    scheme
        The environment's storage scheme, one of `ROOT_SCHEME_EXAMPLES`.

    Raises
    ------
    ValidationError
        If the root's scheme differs (`ROOT_SCHEME_MESSAGE`, which names both schemes and a
        root in the storage's scheme).
    """
    root_scheme, sep, _ = root.partition("://")
    lowered = root_scheme.lower()
    if sep and SCHEME_ALIASES.get(lowered, lowered) != scheme:
        raise ValidationError(
            ROOT_SCHEME_MESSAGE.format(
                root=root,
                root_scheme=root_scheme,
                scheme=scheme,
                example=ROOT_SCHEME_EXAMPLES[scheme],
            )
        )


def root_base(root: str) -> str:
    """Write a storage root without its trailing slashes, so that ``base/name`` is a child of it.

    Only the slashes of the path go, never those of ``scheme://``: the root of a disk,
    ``file:///``, becomes ``file://``, so ``file:///managed`` is its ``managed/`` (a plain
    ``rstrip("/")`` would make it ``file:``).

    Parameters
    ----------
    root
        A storage root, with or without trailing slashes.

    Returns
    -------
    str
        The root to join a relative path to with one ``/``.
    """
    scheme, sep, rest = root.partition("://")
    return f"{scheme}{sep}{rest.rstrip('/')}" if sep else root.rstrip("/")


def check_storage_root(root: str) -> None:
    """Refuse a storage root that is not written canonically.

    Every table location is built from the root, so a root in another spelling of a place
    (``file:////data``, ``.../a//b``, ``FILE://``, ``s3a://``) gives every location of the
    environment a spelling that the location checks cannot compare with the others. One
    trailing slash is accepted; locations are built without it.

    Parameters
    ----------
    root
        The root, ``scheme://path``.

    Raises
    ------
    ValidationError
        If the root contains ``::`` anywhere (`ROOT_CHAIN_MESSAGE`: fsspec would read it as a
        chain, the part before ``::`` alone, so ``file:///a/managed::memory:/dev`` would be
        ``/a/managed``), if a ``file`` root is the root of the disk in any spelling (``file:///``, ``file://``,
        ``file://localhost/``, ``file:////``; `ROOT_DISK_MESSAGE`: the storage backend would
        resolve it against the working directory), relative or has a host
        (`ROOT_RELATIVE_MESSAGE`; ``localhost``
        is named with the spelling to use), it cannot be normalised exactly
        (`ROOT_UNJUDGEABLE_MESSAGE`), an ``s3`` or ``abfs`` root (aliases and case
        included) names no bucket or container (``s3://``, ``s3:///data``, ``abfs:///x``;
        `ROOT_BUCKET_MESSAGE`, which shows the shape to use: s3fs drops leading slashes, so
        ``s3:///data`` is bucket ``data``), or its canonical spelling differs
        (`ROOT_SPELLING_MESSAGE`, which names that spelling).
    """
    if FSSPEC_CHAIN in root:
        raise ValidationError(ROOT_CHAIN_MESSAGE.format(root=root))
    scheme, sep, rest = root.partition("://")
    lowered = scheme.lower()
    if lowered == "file" and sep:
        host, _, tail = rest.partition("/")
        if host.lower() in {"", "localhost"} and normalised_path("/" + tail) == "/":
            raise ValidationError(ROOT_DISK_MESSAGE.format(root=root))
    if lowered == "file" and not rest.startswith("/"):
        host, slash, tail = rest.partition("/")
        if host.lower() == "localhost" and slash:
            raise ValidationError(
                ROOT_SPELLING_MESSAGE.format(
                    root=root, canonical=f"file:///{normalised_path(tail) or ''}"
                )
            )
        raise ValidationError(ROOT_RELATIVE_MESSAGE.format(root=root))
    path = normalised_path(rest)
    if (
        not sep
        or path is None
        or PERCENT_ESCAPE.search(rest)
        or any(char in rest for char in _GLOB_METACHARACTERS)
    ):
        raise ValidationError(ROOT_UNJUDGEABLE_MESSAGE.format(root=root))
    canonical_scheme = SCHEME_ALIASES.get(lowered, lowered)
    if canonical_scheme in _BUCKET_SCHEMES and (not path or path.startswith("/")):
        raise ValidationError(
            ROOT_BUCKET_MESSAGE.format(root=root, example=ROOT_SCHEME_EXAMPLES[canonical_scheme])
        )
    canonical = f"{canonical_scheme}://{path}"
    if root not in {canonical, canonical + "/"}:
        raise ValidationError(ROOT_SPELLING_MESSAGE.format(root=root, canonical=canonical))


def is_within_location(location: str, uri: str) -> bool:
    """Check whether `uri` is a canonical absolute URI lying under `location`.

    Shared by `lakana_core.catalog.service.CatalogService.commit_table` (a
    commit-time check on an `AddSnapshotUpdate`'s `manifest_list`) and
    `lakana_worker.pipeline` (query-time checks on data file, manifest, and
    manifest-list paths) — both need the identical containment rule, and
    `lakana-core` must not import `lakana-worker`, so the rule lives here and
    `lakana-worker` imports it, never the reverse.

    The comparison requires a path-segment boundary, not a bare string
    prefix: `location` is compared with exactly one trailing slash added
    (after stripping any it already has), so a sibling directory that merely
    shares `location` as a string prefix — for example
    ``.../orders-<hex>2/...`` against ``.../orders-<hex>`` — does not count
    as being inside it.

    `uri` must also be canonical (`_is_canonical_uri`): its path component,
    split from the ``scheme://`` prefix, must contain no glob metacharacter
    (`*`, `?`, `[`, `]`) and must equal its own
    `posixpath.normpath`. The glob check closes a bypass the canonical-path
    check alone does not: DuckDB expands a glob in a local path itself, so a
    "file path" like ``.../managed/*/*/data/*.parquet`` contains no `.`/`..`
    segment and is already `posixpath.normpath`-stable, yet would let a
    single manifest entry match every table's data files once opened. The
    `.`/`..`/doubled-slash check rejects `.` and `..` segments and doubled
    slashes, which would otherwise let a `uri` whose *textual* prefix
    matches `location` actually resolve somewhere else once a storage
    backend's own path normalisation runs on it later (`FsspecStorage._full`
    calls `posixpath.normpath` on every root-relative path it is given) —
    for example ``<attacker's own location>/../<victim's
    directory>/data/x.parquet``, which starts with the attacker's own
    location as a bare string but resolves to the victim's directory. A
    non-canonical `uri` is refused outright rather than normalised and
    re-checked, so there is never a gap between the string this function
    checks and the path a reader would actually open.

    A `uri` using a different scheme, or a different spelling of a scheme
    that in fact addresses the same backend (`s3a://` against `s3://`, or a
    percent-encoded character against its literal form), is not specially
    detected — it simply fails the literal string-prefix match against
    `location` and so is refused, fail closed rather than fail open.

    Parameters
    ----------
    location
        A table's own root location.
    uri
        An absolute URI to check — a data file, a manifest, or a manifest
        list.

    Returns
    -------
    bool
        `True` if `uri` is canonical and lies under `location`.
    """
    if not _is_canonical_uri(uri):
        return False
    return uri.startswith(location.rstrip("/") + "/")


def is_id_suffixed_location(location: str, table_id: uuid.UUID) -> bool:
    """Check whether `location` is canonical and its final path segment names `table_id`.

    This fails closed. A table
    created before this scheme existed (or before its `confine_to_root`
    predecessor) can have a location that is the environment root or an
    intermediate namespace directory, and the set of "other" table
    locations `is_owned_by_location`/`nested_locations_within` compares
    against cannot be complete: an `EXTERNAL` table's `raw/` claim and a
    dropped table's orphaned data files are never in it, since only
    currently-existing `ICEBERG` rows contribute a location. Rather than
    trying to complete that set, the worker refuses to read *any* table
    whose own location does not already carry the exclusivity guarantee
    `ResolvedStorage.table_location` assigns at creation — an id-suffixed
    leaf directory. A table failing this check must be re-created (there is
    no in-place migration for an existing table's location); this closes
    the location half of the id binding unconditionally, independent of what other
    tables exist or ever existed in the environment.

    Parameters
    ----------
    location
        The table's own root location, as recorded in its current metadata.
    table_id
        The table's id (the catalog row's id, equal to the Iceberg
        ``table-uuid`` for every table created since locations carry the table's id).

    Returns
    -------
    bool
        `True` if `location` is canonical and its final ``/``-separated
        path segment ends with ``-`` followed by `table_id`'s hex digits.
    """
    if not _is_canonical_uri(location):
        return False
    final_segment = location.rstrip("/").rsplit("/", 1)[-1]
    return final_segment.endswith(f"-{table_id.hex}")


def nested_locations_within(location: str, other_locations: Sequence[str]) -> list[str]:
    """Filter `other_locations` to the ones nested inside `location`.

    Precomputing this once per table, rather than re-deriving it for every
    data file the table's snapshot lists, is the point of splitting it out
    : the filter does not depend
    on which file is being checked, only on `location` and the fixed set of
    other tables in the environment, so redoing it per file was pure waste
    on any table with more than one data file.

    Parameters
    ----------
    location
        The table's own root location.
    other_locations
        Every other table's root location in the same environment.

    Returns
    -------
    list of str
        The subset of `other_locations` that `is_within_location` places
        inside `location`. Empty if none are, including when `location`
        itself appears in `other_locations` — a location is never "within"
        itself, having no trailing content past the boundary.
    """
    return [other for other in other_locations if is_within_location(location, other)]


def is_owned_by_location_given_nested(
    location: str, uri: str, nested_locations: Sequence[str]
) -> bool:
    """Decide whether `uri` belongs to `location`, given its precomputed nested locations.

    `nested_locations` must already be filtered to locations nested inside
    `location` (`nested_locations_within`) — this function does not
    re-derive that filter, so passing a location that is not actually
    nested inside `location` (a sibling, or a broader parent) can make it
    wrongly reject `uri` as if a location outside `location` could be "more
    specific" than it. `is_owned_by_location` is the safe, self-contained
    entry point when there is no precomputed list to reuse.

    Parameters
    ----------
    location
        The table's own root location.
    uri
        An absolute data file URI to check.
    nested_locations
        Other table locations already known to lie inside `location`, from
        `nested_locations_within`.

    Returns
    -------
    bool
        `True` if `uri` lies within `location`, and no location in
        `nested_locations` also contains `uri`.
    """
    if not is_within_location(location, uri):
        return False
    return not any(is_within_location(other, uri) for other in nested_locations)


def is_owned_by_location(location: str, uri: str, other_locations: Sequence[str]) -> bool:
    """Decide whether `uri` belongs to the table at `location`, not a more specific one.

    A table created before locations carried the table's id can have a location that is the
    environment root or an intermediate namespace directory — the earlier
    `confine_to_root` check allowed any in-root value, not only an
    id-suffixed leaf directory. Such a location can therefore textually
    contain another, unrelated table's own directory: not because that table
    is a child of the legacy one, but simply because of where namespaces and
    id-suffixed table directories happen to land in storage. A file that
    actually belongs to that nested, more specific table must not be
    attributed to the outer, less specific one merely because it also lies
    within the outer location — the most specific owner wins.

    This is the simple, self-contained form: it re-derives
    `nested_locations_within` on every call. `lakana_worker.pipeline` instead
    calls `nested_locations_within` once per table and
    `is_owned_by_location_given_nested` once per file, since the nested set
    does not change between files.

    Parameters
    ----------
    location
        The table's own root location.
    uri
        An absolute data file URI to check.
    other_locations
        Every other table's root location in the same environment. Passing
        `location` itself, or a location unrelated to it, changes nothing:
        `is_within_location` on either is `False` (a location is never
        "within" itself, having no trailing content past the boundary), so
        neither is ever treated as the more specific owner.

    Returns
    -------
    bool
        `True` if `uri` lies within `location`, and no location in
        `other_locations` that is itself nested inside `location` also
        contains `uri`.
    """
    return is_owned_by_location_given_nested(
        location, uri, nested_locations_within(location, other_locations)
    )


def _parse_scheme(root: str) -> Literal["file", "s3", "abfs"]:
    """Extract and validate the scheme prefix of a storage root URI.

    Parameters
    ----------
    root
        A storage root URI, such as ``file:///var/lakana/dev``.

    Returns
    -------
    {"file", "s3", "abfs"}
        The URI's scheme.

    Raises
    ------
    ValidationError
        If the URI has no recognised scheme.
    """
    scheme = root.split("://", 1)[0]
    if scheme == "file":
        return "file"
    if scheme == "s3":
        return "s3"
    if scheme == "abfs":
        return "abfs"
    raise ValidationError(f"Unsupported storage scheme {scheme!r} in root {root!r}.")


def root_never_resolves(root: str) -> bool:
    """Say whether a stored root is one that no Lakana release ever reached.

    True only for a scheme `_parse_scheme` refuses (``local://``, ``memory://``, ``gs://``:
    older releases stored them, and every release refused to open them), so no file can be
    behind it. Not for an fsspec chain (``::``, `ROOT_CHAIN_MESSAGE`): releases before 0.18.0
    opened the part before it and wrote files there. Not for a secret, a key or an option that
    makes `for_environment` fail either: it can be fixed, and the storage may hold every file.

    Parameters
    ----------
    root
        The environment's stored storage root.

    Returns
    -------
    bool
        Whether no file can be behind the root.
    """
    try:
        _parse_scheme(root)
    except ValidationError:
        return True
    return False


@dataclass(frozen=True)
class ResolvedStorage:
    """An environment's storage, ready to use.

    Parameters
    ----------
    config
        The configuration this was built from, including any secret merged
        in by `StorageResolver.for_environment`. This dataclass's own
        `repr` is auto-generated from its fields, so it inherits
        `StorageConfig.__repr_args__`'s redaction of `options` values —
        it never needs its own override for that reason. `storage` and
        `file_io` need none either: neither `Storage`/`FsspecStorage` nor
        pyiceberg's `FileIO` implementations override `__repr__`, so both
        print only their default, identity-only object repr.
    storage
        Lakana's own view of the backend.
    file_io
        pyiceberg's view of the same backend, built from the same config so
        there is a single source of storage truth.
    """

    config: StorageConfig
    storage: Storage
    file_io: FileIO

    def table_location(self, ns: NamespacePath, table: str, table_id: uuid.UUID) -> str:
        """Return the root location for a table's data and metadata.

        This is the choke point where a namespace and a table name become a
        single absolute URI handed straight to `file_io` — never to
        `Storage`, so `Storage`'s own confinement guard is not on this path.
        `ns` was already validated component-by-component when it was built
        by `NamespacePath.from_parts`, but `table` was not: a table name is
        not a namespace component and is never wrapped in a `NamespacePath`,
        so this method must validate it independently, or a value such as
        ``"../../pwn"`` would reach `file_io` and escape the environment's
        storage root.

        The location is suffixed with `table_id`'s hex digits,
        because `rename_table` does not move files: without the suffix, a
        new table created under a name a previous table was renamed away
        from would be assigned that previous table's old directory, and its
        data files would then pass the worker's containment check
        (:func:`is_within_location`) as if they belonged to it.

        Parameters
        ----------
        ns
            The namespace containing the table.
        table
            The table's name.
        table_id
            The table's id, suffixed onto the directory name so each
            table's location is unique for the table's lifetime.

        Returns
        -------
        str
            An absolute URI beneath the environment's storage root.

        Raises
        ------
        ValidationError
            If `table` is empty, whitespace-only, ``.``, ``..``, or contains
            ``.``, ``/`` or the unit separator, or ``%`` (only a new table gets a location:
            `refuse_percent_in_new_name`).
        """
        validate_path_component(table, what="Table name")
        refuse_percent_in_new_name(table, what="Table name")
        parts = "/".join(ns.parts)
        return f"{self.config.root.rstrip('/')}/{MANAGED_PREFIX}/{parts}/{table}-{table_id.hex}"

    def confine_to_root(self, location: str) -> str:
        """Validate that a caller-supplied table location lies within the storage root.

        `create_table` normally derives a table's root location from
        :meth:`table_location`, the sole choke point between a namespace,
        a table name, and `file_io`. The Iceberg REST `CreateTableRequest`
        also allows a client to specify the location explicitly, which
        bypasses that choke point entirely: the value arrives as an
        already-absolute URI, handed straight to `file_io`, with no
        namespace or table name of its own for `table_location` to
        validate. This method is the confinement check that stands in for
        it on that path, so a caller-supplied location can never point
        outside this environment's storage root, whether by naming an
        unrelated path outright or by using a ``..`` segment to climb out
        of it.

        Parameters
        ----------
        location
            The caller-supplied location to validate.

        Returns
        -------
        str
            `location`, unchanged, once confirmed to lie inside the root.

        Raises
        ------
        ValidationError
            If `location` contains a NUL byte; if `location`'s scheme
            differs from the root's; or if its normalised path does not
            lie inside the root's path on a path-segment boundary (so a
            sibling such as ``.../<root>evil`` cannot pass as being inside
            ``.../<root>``); or if it lies inside the root's ``artifacts/``
            directory, which is reserved for artifacts (the artifact sweep
            treats every directory there as an artifact's). Rejecting the NUL byte here, as
            `FsspecStorage._full` already does for root-relative paths,
            keeps this the one path where a caller-supplied location does
            not otherwise reach `file_io` and surface as a raw third-party
            exception (`pyarrow`'s `ArrowInvalid`) instead of `lakana-core`'s
            own `ValidationError`.
        """
        if "\x00" in location:
            raise ValidationError(f"Location contains a NUL byte: {location!r}.")
        root = self.config.root
        root_scheme, root_sep, root_rest = root.partition("://")
        loc_scheme, loc_sep, loc_rest = location.partition("://")
        if not root_sep or not loc_sep or loc_scheme != root_scheme:
            raise ValidationError(
                f"Location {location!r} is not inside the environment's storage root {root!r}."
            )
        root_path = posixpath.normpath("/" + root_rest.strip("/"))
        loc_path = posixpath.normpath("/" + loc_rest.strip("/"))
        prefix = root_path.rstrip("/") + "/"
        if loc_path != root_path and not loc_path.startswith(prefix):
            raise ValidationError(
                f"Location {location!r} is not inside the environment's storage root {root!r}."
            )
        reserved = prefix + ARTIFACTS_PREFIX
        if loc_path == reserved or loc_path.startswith(reserved + "/"):
            raise ValidationError(
                f"Location {location!r} is inside the directory {ARTIFACTS_PREFIX}/ of the "
                "environment's storage root, which is reserved for artifacts. Choose a "
                "location outside it."
            )
        return location

    def validate_table_properties(self, properties: Mapping[str, str]) -> None:
        """Reject caller-supplied table properties that would assign a table's location.

        Three of pyiceberg's table properties are, in effect, extra location
        parameters that arrive under a different name and are never seen by
        the refusal applied to a table's `location=`: `write.metadata.path`,
        `write.data.path`, and `write.object-storage.path`. All three are
        refused outright here, whatever their value — Lakana assigns every managed table's location
        itself, and a property that could redirect a table's metadata or
        data elsewhere is the same kind of caller-supplied location
        `create_table`'s `location=` argument already refuses, not merely
        confines. A fourth, `write.py-location-provider.impl`, is not a
        location at all but a dotted class path pyiceberg imports and
        instantiates, so it is refused outright for a different reason.

        This lives on `ResolvedStorage` rather than in `create_table`
        because this is where the storage root is, and because every future
        write path — a commit, a metadata update — has the same exposure
        and must be able to make the same single call.

        Parameters
        ----------
        properties
            The caller-supplied table properties to check. Keys this guard
            does not name are ignored.

        Raises
        ------
        ValidationError
            If `properties` sets `write.py-location-provider.impl`,
            `write.metadata.path`, `write.data.path`, or
            `write.object-storage.path`.
        """
        forbidden = sorted(_FORBIDDEN_TABLE_PROPERTIES & properties.keys())
        if forbidden:
            raise ValidationError(
                f"Table properties may not set {', '.join(forbidden)}: "
                "this would import and instantiate a class named by the caller."
            )
        location_properties = sorted(_PATH_TABLE_PROPERTIES & properties.keys())
        if location_properties:
            raise ValidationError(
                f"Table properties may not set {', '.join(location_properties)}: "
                "Lakana assigns table locations."
            )


class StorageResolver:
    """Maps an environment name to its storage.

    Parameters
    ----------
    session
        Database session used to look environments up.
    key_provider
        Decrypts a secret referenced by an environment's
        `storage_secret_ref`. Optional and `None` by default: the
        overwhelming majority of environments (in particular every
        `file`-scheme one) never set `storage_secret_ref`, and requiring
        a real `KeyProvider` for those would force every caller —
        including roughly 57 existing test call sites across this
        repo's test suites — to construct one purely as ceremony. A
        `ValidationError` is raised instead, at the point an environment
        that actually needs one is resolved without it.
    """

    def __init__(self, session: Session, key_provider: KeyProvider | None = None) -> None:
        self._session = session
        self._key_provider = key_provider

    def _resolve_secret_values(self, env: Environment) -> dict[str, str]:
        """Decrypt the secret an environment's storage config references.

        Parameters
        ----------
        env
            The environment whose `storage_secret_ref` names the secret
            to decrypt.

        Returns
        -------
        dict of str to str
            The decrypted key-value bag.

        Raises
        ------
        ValidationError
            If this `StorageResolver` was constructed with no
            `key_provider` at all.
        NotFoundError
            If `env.storage_secret_ref` names no active secret in this
            environment — a dangling reference fails closed here rather
            than silently falling back to plaintext-only options.
        LakanaError
            If the secret cannot be decrypted under the configured master
            key — either because the key itself is wrong
            or because the row was altered, or moved from another secret
            (`decrypt_secret_row` names the secret and says what to do), or
            because it was written by a newer Lakana.
        SecretUseRefused
            If the secret is bound to a package index or a repository.
        """
        if self._key_provider is None:
            raise ValidationError(
                f"Environment {env.name!r} requires decrypting a secret "
                f"({env.storage_secret_ref!r}) to resolve its storage, but no "
                "key_provider was given to this StorageResolver."
            )
        secret = self._session.execute(
            select(Secret).where(
                Secret.environment_id == env.id,
                Secret.name == env.storage_secret_ref,
                Secret.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if secret is None:
            raise NotFoundError(
                f"Environment {env.name!r} references secret {env.storage_secret_ref!r}, "
                "which does not exist or has been revoked."
            )
        require_use(self._session, secret, SecretUse.environment(), environment_name=env.name)
        return decrypt_secret_row(
            secret,
            self._key_provider,
            environment_name=env.name,
            accept_format_1=self._key_provider.accepts_format_1,
        )

    def for_environment(
        self, environment: str, *, include_deleted: bool = False
    ) -> ResolvedStorage:
        """Resolve an environment's storage.

        Parameters
        ----------
        environment
            The environment's name.
        include_deleted
            Resolve a deleted environment's storage too. Only the location sweep, which deletes
            a deleted environment's files after its undo period, and a restore ask for it;
            everything else finds a deleted environment missing.

        Returns
        -------
        ResolvedStorage
            Storage and FileIO for that environment. If `env.storage_secret_ref`
            is set, the referenced secret's decrypted fields are merged
            into `config.options`, winning over any same-named plaintext
            option already there — a secret must never be silently
            shadowed by a stale plaintext value left over from before it
            was configured.

        Raises
        ------
        NotFoundError
            If no environment has that name (or it is deleted, without `include_deleted`),
            or its `storage_secret_ref` names no active secret.
        ValidationError
            If the environment's storage root has an unrecognised scheme,
            or it needs a secret decrypted but this `StorageResolver` was
            given no `key_provider`.
        LakanaError
            If a secret needs decrypting but cannot be, under the
            configured master key (wrong key, or corrupted/tampered
            ciphertext) — see :meth:`_resolve_secret_values`.
        """
        env = self._session.execute(
            select(Environment).where(Environment.name == environment)
        ).scalar_one_or_none()
        if env is None or (env.deleted_at is not None and not include_deleted):
            raise NotFoundError(f"No environment named {environment!r}.")
        options = dict(env.storage_options)
        if env.storage_secret_ref is not None:
            options.update(self._resolve_secret_values(env))
        config = StorageConfig(
            scheme=_parse_scheme(env.storage_root), root=env.storage_root, options=options
        )
        return ResolvedStorage(
            config=config, storage=build_storage(config), file_io=build_file_io(config)
        )


def table_root_from_metadata_location(metadata_location: str) -> str | None:
    """Derive a table's root location from its current metadata file location.

    Parameters
    ----------
    metadata_location
        A table's `metadata_location`, such as
        ``.../managed/gold/orders-<hex>/metadata/00000-<uuid>.metadata.json``.

    Returns
    -------
    str or None
        Everything before the last ``/metadata/`` segment, or `None` when
        there is none — a location this can't derive contributes nothing
        rather than failing the caller.
    """
    marker = "/metadata/"
    index = metadata_location.rfind(marker)
    return None if index == -1 else metadata_location[:index]
