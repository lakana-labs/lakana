"""The artifact manifest: canonical form, limits and path rules.

A manifest lists the files of one artifact version. It is untrusted input:
it comes from a request, and it is read back from the database. Everything
wrong with it ends as a `ValidationError` whose text names the cause and the
next step. `lakana._artifact_manifest` holds the same path rules for the
SDK, which imports nothing of core; `test_artifact_manifest_parity.py` keeps
the two alike.

The character classes, case folding and normalisation used here come from the
Unicode tables of the Python that runs them. An SDK and a server on different
Python versions can therefore disagree about a newly assigned character. The
server's answer is the one that counts; a disagreement ends as a refused
upload, never as a path that is stored unchecked.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from lakana_core.bounds import SHA256_HEX
from lakana_core.config import ArtifactsConfig
from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown

MANIFEST_FORMAT = 1
MAX_PATH_BYTES = 1024
MAX_PATH_SEGMENTS = 32
MAX_SEGMENT_BYTES = 255

SHA256_PATTERN = SHA256_HEX
"""A SHA-256 digest as the store and the manifest write it: 64 lowercase hex digits."""
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{device}{digit}" for device in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"]
)
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_KEYS = {"files", "format", "is_directory"}
_FILE_KEYS = {"path", "sha256", "size"}
_ADVICE = "ask the operator to raise the setting."


def path_problem(path: object) -> str | None:
    """Say what is wrong with one manifest path.

    Parameters
    ----------
    path
        The path as given.

    Returns
    -------
    str or None
        The problem, worded to follow ``Path <path> ``; `None` for a relative,
        ``/``-separated path that passes these rules, checked in this order
        (the first failure is returned): a non-empty string; no control,
        format or surrogate character or line separator; no backslash; no
        leading ``/``; no empty, ``.`` or ``..`` segment; then, segment by
        segment, no ``:``, no ending in ``.`` or a space, and no name Windows
        reserves (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM0`` to ``COM9``,
        ``LPT0`` to ``LPT9`` and the superscript forms, ``CONIN$`` and ``CONOUT$``,
        compared as Windows does, with ASCII-only upper-casing, and with or
        without an extension: the name is the part before the first
        ``.``); then at most `MAX_PATH_BYTES` bytes, no segment of more than
        `MAX_SEGMENT_BYTES` bytes (UTF-8, as a file system counts a name) and
        at most `MAX_PATH_SEGMENTS` segments.
    """
    if not isinstance(path, str) or not path:
        return "a path must be a non-empty string"
    if any(unicodedata.category(char) in _UNSAFE for char in path):
        return "must not contain a control, format or surrogate character or a line separator"
    if "\\" in path:
        return "must not contain a backslash; separate segments with '/'"
    if path.startswith("/"):
        return "must be relative, without a leading '/'"
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return "must not have an empty, '.' or '..' segment"
    for segment in segments:
        if ":" in segment:
            return "must not contain ':'"
        if segment.endswith((".", " ")):
            return "must not have a part ending in '.' or a space"
        if segment.split(".", 1)[0].rstrip(" ").translate(_ASCII_UPPER) in _RESERVED_NAMES:
            return "must not use a name Windows reserves, such as 'NUL'"
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        return f"is longer than {MAX_PATH_BYTES} bytes"
    if any(len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES for segment in segments):
        return f"must not have a part longer than {MAX_SEGMENT_BYTES} bytes"
    if len(segments) > MAX_PATH_SEGMENTS:
        return f"has more than {MAX_PATH_SEGMENTS} segments"
    return None


def fold_path(path: str) -> str:
    """Fold a path the way a case- and normalisation-insensitive file system does.

    Parameters
    ----------
    path
        A valid manifest path.

    Returns
    -------
    str
        ``NFD(casefold(NFD(path)))``; two paths such a file system treats as
        one fold equal.
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def paths_problem(paths: Sequence[str]) -> str | None:
    """Say why a set of valid paths cannot be one tree on every file system.

    Parameters
    ----------
    paths
        Paths that each passed `path_problem`.

    Returns
    -------
    str or None
        A whole sentence naming two files, or two directories, that differ
        only by case or Unicode normalisation, or a path that is both a file
        and a directory; `None` when the paths are one unambiguous tree.
    """
    files: dict[str, str] = {}
    directories: dict[str, str] = {}
    seen: set[str] = set()
    for path in paths:
        segments = path.split("/")
        raw = ""
        folded = ""
        for segment in segments[:-1]:
            raw = f"{raw}/{segment}" if raw else segment
            folded = f"{folded}/{fold_path(segment)}" if folded else fold_path(segment)
            if raw in seen:
                continue
            seen.add(raw)
            known = directories.setdefault(folded, raw)
            if known != raw:
                return (
                    f"Directories {shown(known)} and {shown(raw)} differ only by case or "
                    "Unicode normalisation; rename one."
                )
        folded_path = f"{folded}/{fold_path(segments[-1])}" if folded else fold_path(segments[-1])
        other = files.setdefault(folded_path, path)
        if other != path:
            return (
                f"Paths {shown(other)} and {shown(path)} differ only by case or "
                "Unicode normalisation; rename one."
            )
    for folded, raw in files.items():
        if folded in directories:
            return f"{shown(raw)} is both a file and a directory."
    return None


def canonical_manifest(is_directory: bool, files: Sequence[tuple[str, int, str]]) -> bytes:
    """Serialise a manifest in its one canonical form.

    Parameters
    ----------
    is_directory
        Whether the artifact is a directory.
    files
        ``(path, size, sha256)``; sorted here by the path's UTF-8 bytes, so
        the order given does not matter.

    Returns
    -------
    bytes
        ASCII JSON with sorted keys and no whitespace.
    """
    ordered = sorted(files, key=lambda file: file[0].encode("utf-8", "surrogatepass"))
    document = {
        "files": [{"path": path, "sha256": digest, "size": size} for path, size, digest in ordered],
        "format": MANIFEST_FORMAT,
        "is_directory": is_directory,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def manifest_digest(is_directory: bool, files: Sequence[tuple[str, int, str]]) -> str:
    """Hash a manifest's canonical form: the identity of the content.

    Parameters
    ----------
    is_directory
        Whether the artifact is a directory.
    files
        ``(path, size, sha256)``, sorted by the path's UTF-8 bytes.

    Returns
    -------
    str
        The SHA-256, lower-case hex.
    """
    return hashlib.sha256(canonical_manifest(is_directory, files)).hexdigest()


@dataclass(frozen=True)
class ManifestFile:
    """One file of an artifact version.

    Parameters
    ----------
    path
        Its path inside the artifact.
    size
        Its size in bytes.
    sha256
        The SHA-256 of its contents, lower-case hex.
    """

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ManifestLimits:
    """The operator's bounds on one version.

    Parameters
    ----------
    max_file_bytes
        `LAKANA_ARTIFACTS__MAX_FILE_BYTES`.
    max_bytes
        `LAKANA_ARTIFACTS__MAX_BYTES`.
    max_files
        `LAKANA_ARTIFACTS__MAX_FILES`.
    """

    max_file_bytes: int
    max_bytes: int
    max_files: int

    @classmethod
    def from_config(cls, config: ArtifactsConfig) -> ManifestLimits:
        """Take the limits from the settings.

        Parameters
        ----------
        config
            The `LAKANA_ARTIFACTS__*` settings.

        Returns
        -------
        ManifestLimits
            The limits.
        """
        return cls(config.max_file_bytes, config.max_bytes, config.max_files)


@dataclass(frozen=True)
class ArtifactManifest:
    """A validated manifest.

    Parameters
    ----------
    is_directory
        Whether the artifact is a directory; `False` for a single file.
    files
        The files, sorted by the path's UTF-8 bytes.
    """

    is_directory: bool
    files: tuple[ManifestFile, ...]

    def _tuples(self) -> list[tuple[str, int, str]]:
        """List the files as the canonical form takes them.

        Returns
        -------
        list of tuple
            ``(path, size, sha256)``.
        """
        return [(file.path, file.size, file.sha256) for file in self.files]

    def to_json(self) -> dict[str, object]:
        """Return the manifest as a JSON-serialisable document.

        Returns
        -------
        dict
            ``format``, ``is_directory`` and ``files``.
        """
        return {
            "format": MANIFEST_FORMAT,
            "is_directory": self.is_directory,
            "files": [
                {"path": file.path, "size": file.size, "sha256": file.sha256} for file in self.files
            ],
        }

    def canonical(self) -> bytes:
        """Return the canonical bytes, which are also the stored manifest object.

        Returns
        -------
        bytes
            `canonical_manifest` of this manifest.
        """
        return canonical_manifest(self.is_directory, self._tuples())

    def sha256(self) -> str:
        """Return the manifest's digest.

        Returns
        -------
        str
            `manifest_digest` of this manifest.
        """
        return manifest_digest(self.is_directory, self._tuples())

    @property
    def size_bytes(self) -> int:
        """The sum of the files' sizes.

        Returns
        -------
        int
            Bytes.
        """
        return sum(file.size for file in self.files)

    @property
    def file_count(self) -> int:
        """The number of files.

        Returns
        -------
        int
            The count.
        """
        return len(self.files)

    def hashes(self) -> dict[str, int]:
        """Map each distinct hash to its size.

        Returns
        -------
        dict
            SHA-256 to bytes.
        """
        return {file.sha256: file.size for file in self.files}


def _format_label(version: object) -> str:
    """Say a manifest's `format` value in a message without trusting its size.

    Parameters
    ----------
    version
        The value as given.

    Returns
    -------
    str
        A short integer or string as it is, otherwise the type's name.
    """
    if type(version) is int and abs(version) < _SIZE_SHOWN_BELOW:
        return str(version)
    if isinstance(version, str):
        return f"'{shown(version)}'"
    return type(version).__name__


_SIZE_SHOWN_BELOW = 10**20


def _bytes(count: int) -> str:
    """Say a byte count in a message without formatting an unbounded integer.

    Parameters
    ----------
    count
        A size or a total, 0 or more.

    Returns
    -------
    str
        ``<count> bytes``, or ``more than 10^20 bytes`` from 10^20 up.
    """
    if count < _SIZE_SHOWN_BELOW:
        return f"{count} bytes"
    return "more than 10^20 bytes"


def _is_size(value: object) -> bool:
    """Say whether a JSON value is a file size.

    Parameters
    ----------
    value
        A decoded JSON value.

    Returns
    -------
    bool
        Whether it is an `int` (not a `bool`) of 0 or more.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _file(entry: object, limits: ManifestLimits | None) -> ManifestFile:
    """Validate one file entry.

    Parameters
    ----------
    entry
        The entry as given.
    limits
        The operator's bounds, or `None` to check the shape only.

    Returns
    -------
    ManifestFile
        The file.

    Raises
    ------
    ValidationError
        For a wrong shape, a bad path, or a file over `max_file_bytes`.
    """
    if not isinstance(entry, dict):
        raise ValidationError(
            "Each file in a manifest must be an object with 'path', 'size' and 'sha256'."
        )
    path = entry.get("path")
    problem = path_problem(path)
    if problem == "a path must be a non-empty string":
        raise ValidationError("A manifest path must be a non-empty string.")
    if problem is not None:
        raise ValidationError(f"Path {shown(path)} {problem}.")
    assert isinstance(path, str)  # nosec B101 -- path_problem passed
    size, digest = entry.get("size"), entry.get("sha256")
    if (
        set(entry) != _FILE_KEYS
        or not _is_size(size)
        or not isinstance(digest, str)
        or SHA256_PATTERN.match(digest) is None
    ):
        raise ValidationError(
            f"File {shown(path)} must have exactly the keys path, sha256 and size, a size of 0 "
            "or more and a lower-case SHA-256."
        )
    assert isinstance(size, int)  # nosec B101 -- _is_size passed
    if limits is not None and size > limits.max_file_bytes:
        raise ValidationError(
            f"{shown(path)} is {_bytes(size)}, more than LAKANA_ARTIFACTS__MAX_FILE_BYTES "
            f"({limits.max_file_bytes}). Store a smaller file, or {_ADVICE}"
        )
    if size >= _SIZE_SHOWN_BELOW:
        raise ValidationError(
            f"File {shown(path)} is 10^20 bytes or more, which no artifact can hold. "
            "Store a smaller file."
        )
    return ManifestFile(path=path, size=size, sha256=digest)


def parse_manifest(document: object, limits: ManifestLimits | None = None) -> ArtifactManifest:
    """Validate a manifest document.

    Parameters
    ----------
    document
        The decoded JSON; untrusted.
    limits
        The operator's bounds. `None` checks the shape and the paths only,
        for a manifest read back from the database.

    Returns
    -------
    ArtifactManifest
        The manifest.

    Raises
    ------
    ValidationError
        For anything wrong with it; the text names the cause and the next step.
    """
    if (
        not isinstance(document, dict)
        or set(document) != _KEYS
        or not isinstance(document["is_directory"], bool)
        or not isinstance(document["files"], list)
    ):
        raise ValidationError(
            "The manifest must be an object with exactly the keys files, format and is_directory."
        )
    version = document["format"]
    if type(version) is not int or version != MANIFEST_FORMAT:
        raise ValidationError(
            f"Manifest format {_format_label(version)} is not supported; this server reads format "
            f"{MANIFEST_FORMAT}. Upgrade the lakana package that wrote it, or this server."
        )
    entries = document["files"]
    if not entries:
        raise ValidationError("An artifact must contain at least one file.")
    if limits is not None and len(entries) > limits.max_files:
        raise ValidationError(
            f"The artifact has {len(entries)} files, more than LAKANA_ARTIFACTS__MAX_FILES "
            f"({limits.max_files}). Store fewer files, or {_ADVICE}"
        )
    files = tuple(_file(entry, limits) for entry in entries)
    keys = [file.path.encode("utf-8") for file in files]
    if any(left >= right for left, right in pairwise(keys)):
        raise ValidationError("The manifest's files must be sorted by path, without repeats.")
    is_directory = document["is_directory"]
    if not is_directory and (len(files) != 1 or "/" in files[0].path):
        raise ValidationError(
            "A single-file artifact must have exactly one file whose path is a file name."
        )
    collision = paths_problem([file.path for file in files])
    if collision is not None:
        raise ValidationError(collision)
    sizes: dict[str, ManifestFile] = {}
    for file in files:
        first = sizes.setdefault(file.sha256, file)
        if first.size != file.size:
            raise ValidationError(
                f"Files {shown(first.path)} and {shown(file.path)} have the same SHA-256 and "
                "different sizes."
            )
    manifest = ArtifactManifest(is_directory=is_directory, files=files)
    if limits is not None and manifest.size_bytes > limits.max_bytes:
        raise ValidationError(
            f"The artifact is {_bytes(manifest.size_bytes)}, more than LAKANA_ARTIFACTS__MAX_BYTES "
            f"({limits.max_bytes}). Store less, or {_ADVICE}"
        )
    return manifest
