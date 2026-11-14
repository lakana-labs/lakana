"""The artifact part of the task harness: the attempt's directories, the inputs and the upload.

`lakana.runner` makes the directories with `workspace` before any repository code runs,
downloads each artifact input with `fetch_artifact`, checks what an artifact asset returned with
`pin` and stores it with `upload_pinned`. Everything below the attempt's directories is reached
from descriptors the harness opened itself: the output directory's (`Output`) and the inputs
directory's (`Workspace.inputs_descriptor`), never by a path the asset's code could swap, and
never relative to the working directory.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, cast

from lakana import assets
from lakana._artifact_manifest import BY_DESCRIPTOR, DIR_FLAGS, shown
from lakana.errors import LakanaError

if TYPE_CHECKING:
    from lakana._models import ArtifactVersion
    from lakana.client import Client
    from lakana.runner import TaskDescription


class InvalidResult(Exception):
    """The asset returned something that cannot replace its table. Not retryable."""


class InputError(Exception):
    """An input could not be read through the query API or downloaded."""


class ArtifactInputError(InputError):
    """An artifact input could not be downloaded.

    Kept apart from a failed table read because a 404 means something else here: the
    version the worker resolved may have been pruned since (another run's producer
    committed), and the next attempt resolves again.
    """


class WorkspaceError(Exception):
    """The attempt's directories are not what the harness made. Not retryable."""


@dataclass(frozen=True)
class Output:
    """The output directory of an artifact asset, pinned by a descriptor.

    Parameters
    ----------
    path
        `lakana.output_dir()`: the real path of ``<work_dir>/output``.
    descriptor
        The directory opened without following a link right after it was made,
        before any repository code ran. Everything the harness checks and
        stores is reached from it, never by the path again.
    identity
        The device and inode of that directory. Code of the asset may close the
        descriptor, and the number then goes to the next file opened in the
        process, so the harness checks, before it uses or closes the number, that it
        refers to a directory with this device and inode (`is_held`). That proves
        the number is not a file or another directory that still exists; it does not
        prove it is the same directory object in time: after a delete and re-create
        on a file system that reuses inode numbers, a new directory can match. Such
        a directory is always one the task made itself inside its own attempt.
    """

    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class Workspace:
    """The directories the harness made for this attempt.

    Parameters
    ----------
    inputs
        ``<work_dir>/inputs`` when an input is an artifact.
    inputs_identity
        Its device and inode when it was made; a descriptor held on it for the
        attempt keeps the inode from being reused.
    inputs_descriptor
        That descriptor: every download goes into the directory it names, never
        into whatever the path names by then.
    output
        The output directory of an artifact asset.
    """

    inputs: Path | None = None
    inputs_identity: tuple[int, int] | None = None
    inputs_descriptor: int | None = None
    output: Output | None = None


def is_held(descriptor: int, identity: tuple[int, int]) -> bool:
    """Say whether a descriptor refers to a directory with the identity the harness recorded.

    This is a check of device and inode, not of history: a directory the task deleted and
    re-created can have the same pair on a file system that reuses inode numbers.

    A descriptor number the asset's code closed goes to the next file the process
    opens, so the number alone says nothing about what it names now.

    Parameters
    ----------
    descriptor
        The number the harness holds.
    identity
        The device and inode of the directory it opened under that number.

    Returns
    -------
    bool
        ``True`` if the number is open and names a directory with that identity;
        ``False`` if it is closed or names anything else.
    """
    try:
        info = os.fstat(descriptor)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity


def _identity_of(descriptor: int, held: list[tuple[int, tuple[int, int]]]) -> tuple[int, int]:
    """Read the identity of a directory the harness just opened and put it on the list.

    If reading it fails, the descriptor is closed here, so it cannot leak: the clean-up of
    `workspace` only knows what is on `held`.

    Parameters
    ----------
    descriptor
        The directory just opened.
    held
        The descriptors the clean-up closes, with their identities; appended to.

    Returns
    -------
    tuple[int, int]
        Its device and inode.
    """
    try:
        info = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    identity = (info.st_dev, info.st_ino)
    held.append((descriptor, identity))
    return identity


def _make_directory(path: Path) -> None:
    """Make a private directory that must not exist yet.

    Parameters
    ----------
    path
        Where.

    Raises
    ------
    OSError
        If it exists or cannot be made; the text shows the path escaped and bounded.
    """
    try:
        os.mkdir(path, 0o700)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise OSError(exc.errno, f"could not create {shown(str(path))}: {reason}") from exc


@contextlib.contextmanager
def make_workspace(description: TaskDescription) -> Generator[Workspace]:
    """Make the attempt's directories before any repository code runs.

    Parameters
    ----------
    description
        The task description.

    Yields
    ------
    Workspace
        What was made. On exit `lakana.output_dir()` is unset and the descriptors
        of the output and the inputs directory are closed while they still are the
        harness's (`is_held`), whatever happened.

    Raises
    ------
    WorkspaceError
        For an asset that reads or makes an artifact, on a platform that cannot open files
        relative to a directory without following links; nothing is made before this.
    OSError
        If a directory cannot be made (``inputs`` or ``output`` exists already,
        or the disk is full).
    """
    held: list[tuple[int, tuple[int, int]]] = []
    try:
        inputs = identity = pinned = output = None
        work = Path(os.path.realpath(description.work_dir))
        if not BY_DESCRIPTOR and (
            "artifact" in description.input_kinds.values() or description.output == "artifact"
        ):
            raise WorkspaceError(
                "an artifact asset needs a platform that opens files relative to a "
                "directory without following links (Linux or macOS); run the worker there"
            )
        if "artifact" in description.input_kinds.values():
            inputs = work / "inputs"
            _make_directory(inputs)
            # Held open until the attempt ends: while it is, the file system cannot give
            # this directory's inode to another one, so a directory made in its place
            # after an rmdir never has the same identity.
            pinned = os.open(inputs, DIR_FLAGS)
            identity = _identity_of(pinned, held)
        if description.output == "artifact":
            path = work / "output"
            _make_directory(path)
            descriptor = os.open(path, DIR_FLAGS)
            output = Output(path, descriptor, _identity_of(descriptor, held))
            assets._set_output_dir(path)
        yield Workspace(inputs, identity, pinned, output)
    finally:
        assets._set_output_dir(None)
        # Code of the asset may have closed these (a library calling `os.closerange`), and the
        # number may since name another file of the process (the result record's, say): close
        # a number only while it refers to a directory with the identity recorded here. A
        # failing close must not replace the attempt's result, nor skip the other close.
        for number, expected in held:
            if is_held(number, expected):
                with contextlib.suppress(OSError):
                    os.close(number)


def fetch_artifact(
    client: Client,
    description: TaskDescription,
    workspace: Workspace,
    parameter: str,
    name: str,
) -> Path:
    """Download one artifact input into the attempt's directory.

    The copy is placed by one rename into a target that does not exist yet, so
    its root is sealed read-only too. The download writes below the descriptor
    the harness opened on ``inputs`` when it made it, never by path, so a
    directory the asset's code moves or replaces by a link meanwhile does not
    redirect it. The path is checked first as well, so that such a swap fails
    the attempt with a clear message instead of handing the function a path
    that no longer leads to the copy.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    workspace
        The attempt's directories.
    parameter
        The function parameter the input is for (checked as a plain
        identifier by `parse_description`).
    name
        The artifact's dotted name.

    Returns
    -------
    Path
        The read-only local copy: a file, or a directory.

    Raises
    ------
    WorkspaceError
        If the ``inputs`` directory is, by a check of its path, no longer the one
        the harness made, or the asset's code closed the harness's descriptor of it.
    ArtifactInputError
        If the download is refused, fails or does not match its manifest (an
        `InputError`).
    """
    version = description.input_versions[parameter]
    inputs = workspace.inputs
    if inputs is None or not _is_directory(inputs, workspace.inputs_identity):
        raise WorkspaceError(
            "the inputs directory of this attempt was replaced while the task ran, so nothing "
            "was downloaded into it; make sure the asset's code (including its threads and "
            "processes) does not change the attempt's directory, then run the task again"
        )
    directory = _held_copy(workspace.inputs_descriptor, workspace.inputs_identity)
    try:
        got = client.artifacts.download(
            description.environment,
            name,
            parameter,
            version=version,
            read_only=True,
            dir_fd=directory,
        )
    except (LakanaError, OSError) as exc:
        reason = str(exc) or type(exc).__name__
        raise ArtifactInputError(
            f"reading input {parameter!r} ({name}, version {version}) failed: {reason}"
        ) from exc
    finally:
        os.close(directory)
    return inputs / got.path


def _held_copy(descriptor: int | None, identity: tuple[int, int] | None) -> int:
    """Copy a descriptor the harness holds, refusing one the asset's code closed or reused.

    The copy is judged, not the number: from here on it is one open file, so a later close or
    reuse of the number by another thread of the asset's code changes nothing.

    Parameters
    ----------
    descriptor
        The number the harness holds.
    identity
        The device and inode of the directory it was opened on.

    Returns
    -------
    int
        A new descriptor of the same directory; the caller closes it.

    Raises
    ------
    WorkspaceError
        If the number is closed or names anything else now.
    """
    closed = WorkspaceError(
        "the asset's code closed a file descriptor the task needs, so no input was downloaded; "
        "do not close descriptors you did not open (for example with os.closerange), then run "
        "the task again"
    )
    if descriptor is None or identity is None:
        raise closed
    try:
        copy = os.dup(descriptor)
    except OSError as exc:
        raise closed from exc
    if not is_held(copy, identity):
        os.close(copy)
        raise closed
    return copy


def _is_directory(path: Path, identity: tuple[int, int] | None) -> bool:
    """Tell whether a path is still the directory that was made.

    Parameters
    ----------
    path
        The directory's path.
    identity
        Its device and inode when it was made.

    Returns
    -------
    bool
        `True` if it is a directory (not a link) with that identity.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity


def _returned_text(result: object) -> str:
    """Read the text of a returned path once, trusting nothing else about the object.

    The object is the user's: its methods may lie, raise or change their answer.
    It is accepted only if its exact type is a subclass of `pathlib.Path`
    (`__class__` is not asked) and `os.fspath` gives a `str`; this text is then
    the only thing the harness looks at.

    Parameters
    ----------
    result
        The function's return value.

    Returns
    -------
    str
        The path as written.

    Raises
    ------
    InvalidResult
        If it is not a `pathlib.Path` with a readable text.
    """
    kind = type(result)
    if issubclass(kind, Path):
        try:
            text = os.fspath(cast(Path, result))
        except Exception:
            text = None
        if type(text) is str:
            return text
    try:
        name = shown(f"{kind.__module__}.{kind.__qualname__}")
    except BaseException:  # the type is the user's: its attributes may raise anything
        name = "an unknown type"
    hint = "; wrap it in pathlib.Path" if issubclass(kind, str) else ""
    raise InvalidResult(
        "an artifact asset must return a pathlib.Path inside lakana.output_dir() (a file, or "
        f"the directory itself), got {name}{hint}"
    )


_DESCRIPTOR_CLOSED = (
    "the asset's code closed a file descriptor the task needs, so nothing was uploaded; do not "
    "close descriptors you did not open (for example with os.closerange), then run the task again"
)


def pin(result: object, output: Output) -> tuple[int, str]:
    """Check what an artifact asset returned and pin the directory it lies in.

    The returned text is judged lexically (``..`` collapsed, so
    ``output/m/../x`` is ``output/x``) against the output directory's real path,
    and then followed from the output directory's descriptor, one component at a
    time, without following a link. What the harness gives the upload is the
    descriptor of the directory to store, or of the directory that holds the one
    file to store, plus a bare name: nothing the user's code can swap by name
    afterwards is looked up again.

    Parameters
    ----------
    result
        The function's return value.
    output
        The attempt's output directory.

    Returns
    -------
    tuple
        An open descriptor the caller must close, and ``"."`` (store that
        directory) or the name of the file in it (store that file).

    Raises
    ------
    InvalidResult
        If it is not a `pathlib.Path` inside the output directory, does not
        exist, passes through a link, was swapped while it was followed, or the
        asset's code closed the harness's descriptor of the output directory.
    """
    text = _returned_text(result)
    base = os.fspath(output.path)
    normal = os.path.normpath(text) if os.path.isabs(text) else ""
    if normal == base:
        parts: list[str] = []
    elif normal.startswith(base + os.sep):
        parts = normal[len(base) + 1 :].split(os.sep)
    else:
        raise InvalidResult(
            f"the asset returned {shown(text)}, which is not inside lakana.output_dir(); "
            "write the artifact's files there and return that directory or a file in it"
        )
    missing = InvalidResult(
        f"the asset returned {shown(text)}, which does not exist; write the file before "
        "returning its path"
    )
    try:
        current = os.dup(output.descriptor)
    except OSError as exc:
        raise InvalidResult(_DESCRIPTOR_CLOSED) from exc
    # The copy is judged, not the number: it is one open file from here on, so a later close
    # or reopen of the number by another thread of the asset's code does not change what is
    # checked and used. A closed number reused for any other file is refused.
    if not is_held(current, output.identity):
        os.close(current)
        raise InvalidResult(_DESCRIPTOR_CLOSED)
    try:
        for index, part in enumerate(parts):
            try:
                info = os.stat(part, dir_fd=current, follow_symlinks=False)
            except (OSError, ValueError) as exc:  # ValueError: a NUL or a lone surrogate
                if isinstance(exc, OSError) and exc.errno == errno.EACCES:
                    _refuse_unopenable(text, exc)
                raise missing from exc
            if stat.S_ISLNK(info.st_mode):
                raise InvalidResult(
                    f"the asset returned {shown(text)}, which passes through a symbolic link; "
                    "an artifact holds only regular files and directories"
                )
            is_directory = stat.S_ISDIR(info.st_mode)
            if index == len(parts) - 1 and not is_directory:
                return current, part
            if not is_directory:
                raise missing
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=current)
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    _refuse_unopenable(text, exc)
                raise InvalidResult(
                    f"the asset returned {shown(text)}, which was replaced by a symbolic link or "
                    "another file while it was checked; make sure no thread or process of the "
                    "asset's code changes the output directory, then run the task again"
                ) from exc
            os.close(current)
            current = child
        return current, "."
    except BaseException:
        os.close(current)
        raise


def _refuse_unopenable(text: str, exc: OSError) -> NoReturn:
    """Refuse a returned path whose directory cannot be used.

    Parameters
    ----------
    text
        The returned path as the function gave it.
    exc
        The error the operating system raised.

    Raises
    ------
    InvalidResult
        Always; its text names the cause and the next step.
    """
    reason = exc.strerror or type(exc).__name__
    raise InvalidResult(
        f"the asset returned {shown(text)}, which cannot be opened ({reason}); "
        "keep the permissions of the directories below lakana.output_dir() as "
        "they were made, then run the task again"
    ) from exc


def upload_pinned(
    client: Client,
    description: TaskDescription,
    sources: Mapping[str, int | None],
    directory: int,
    entry: str,
) -> ArtifactVersion:
    """Upload ``entry`` of a pinned directory, by its descriptor.

    The upload scans, and re-opens what it stores, relative to the descriptor
    (`dir_fd`), never relative to the working directory: a thread of the
    asset's code that changes the working directory, or swaps a directory for
    a link by name, during the upload does not redirect it.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    description
        The task description.
    sources
        What the task read, for lineage.
    directory
        An open descriptor of the directory; closed here.
    entry
        ``"."`` or the name of a file in the directory.

    Returns
    -------
    ArtifactVersion
        The stored version.

    Raises
    ------
    InvalidResult
        If the directory cannot be entered (the asset's code removed its
        permissions); another attempt would meet the same.
    """
    try:
        try:
            os.close(os.open(".", DIR_FLAGS, dir_fd=directory))
        except OSError as exc:
            raise InvalidResult(
                "the directory the asset returned (or lakana.output_dir() itself) is one the "
                "harness cannot enter, so nothing was uploaded; do not remove the "
                "permissions of the output directory in the asset's code, then run the "
                "task again"
            ) from exc
        return client.artifacts.upload(
            description.environment,
            description.table,
            Path(entry),
            sources=dict(sources),
            dir_fd=directory,
        )
    finally:
        os.close(directory)
