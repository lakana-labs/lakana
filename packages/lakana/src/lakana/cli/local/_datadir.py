"""The local-mode data directory: database, lake, master key, lock and pidfiles."""

from __future__ import annotations

import base64
import binascii
import errno
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lakana.cli._state import CliError

_MASTER_KEY_BYTES = 32

MARKER_NAME = ".lakana-local-instance"
"""The file `prepare_data_dir` writes into every local instance's data directory."""

MARKER_TEXT = "lakana local instance\n"
"""The marker's exact content."""


def write_marker(root: Path) -> None:
    """Mark `root` as a local instance's data directory; a marker that is there stays.

    Parameters
    ----------
    root
        The data directory.

    Raises
    ------
    OSError
        If the marker can't be written.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root / MARKER_NAME, flags, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as handle:
        handle.write(MARKER_TEXT)


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the default data directory.

    Parameters
    ----------
    environ
        The environment to read `XDG_DATA_HOME` from; `os.environ` by default.

    Returns
    -------
    Path
        `$XDG_DATA_HOME/lakana/local`, or `~/.local/share/lakana/local`.
    """
    env = environ if environ is not None else os.environ
    if env.get("XDG_DATA_HOME"):
        base = Path(env["XDG_DATA_HOME"])
    else:
        base = Path.home() / ".local" / "share"
    return base / "lakana" / "local"


@dataclass(frozen=True)
class DataDir:
    """A local-mode data directory and its fixed layout.

    Parameters
    ----------
    root
        The directory.
    """

    root: Path

    @property
    def db_path(self) -> Path:
        """The SQLite metadata database.

        Returns
        -------
        Path
            `<root>/lakana.db`.
        """
        return self.root / "lakana.db"

    @property
    def lake(self) -> Path:
        """The storage root for local environments.

        Returns
        -------
        Path
            `<root>/lake`.
        """
        return self.root / "lake"

    @property
    def master_key_path(self) -> Path:
        """The secrets master key file.

        Returns
        -------
        Path
            `<root>/master.key`.
        """
        return self.root / "master.key"

    @property
    def bootstrap_token_path(self) -> Path:
        """Where the server writes a first-administrator token it had to mint.

        Returns
        -------
        Path
            `<root>/bootstrap-token`.
        """
        return self.root / "bootstrap-token"

    @property
    def server_pid(self) -> Path:
        """The server pidfile.

        Returns
        -------
        Path
            `<root>/server.pid`.
        """
        return self.root / "server.pid"

    @property
    def worker_pid(self) -> Path:
        """The worker pidfile.

        Returns
        -------
        Path
            `<root>/worker.pid`.
        """
        return self.root / "worker.pid"

    @property
    def lock_path(self) -> Path:
        """The lock file a running instance holds for its whole lifetime.

        Returns
        -------
        Path
            `<root>/lock`.
        """
        return self.root / "lock"

    @property
    def database_url(self) -> str:
        """The SQLAlchemy URL of the metadata database.

        Returns
        -------
        str
            `sqlite:///<root>/lakana.db`.
        """
        return f"sqlite:///{self.db_path}"

    def env_root(self, name: str) -> str:
        """Return the storage root URL for a local environment.

        Parameters
        ----------
        name
            The environment name.

        Returns
        -------
        str
            `file://<root>/lake/<name>`.
        """
        return f"file://{self.lake / name}"


def resolve_data_dir(path: Path) -> Path:
    """Resolve a `--data-dir` argument to an absolute path.

    Every URL derived from the data directory (the sqlite database URL, an
    environment's `file://` storage root) must be absolute: a cwd-relative
    root written into the catalog would only resolve from the one directory
    the first run happened to start in, making the instance's data
    unreachable from any other cwd.

    Parameters
    ----------
    path
        The data directory, as given on the command line or by
        `default_data_dir`.

    Returns
    -------
    Path
        `path`, with `~` expanded and made absolute. `Path.absolute()`, not
        `Path.resolve()`, so a symlinked leaf is preserved rather than
        silently replaced by its target — `prepare_data_dir`'s own symlink
        check must still see it.
    """
    return path.expanduser().absolute()


def prepare_data_dir(path: Path) -> DataDir:
    """Create (if needed) and check the data directory.

    Parameters
    ----------
    path
        The data directory; resolved to an absolute path first (see
        `resolve_data_dir`), whether or not it exists yet.

    Returns
    -------
    DataDir
        The checked directory, with `lake/` created and the marker file written (the marker
        is what `lakana server reset` requires before it deletes anything).

    Raises
    ------
    CliError
        If `path` (or its parent) exists and is not a directory, if the
        parent isn't writable, if the parent is other-writable without the
        sticky bit, or if the leaf is a symlink, isn't owned by the current
        user, or is accessible to group/other (exit 2 for all of these). If
        an `OSError` not covered by one of those specific checks occurs
        while creating either directory — e.g. an ancestor of `path` that
        is itself a regular file, raising `NotADirectoryError` — it is
        reported the same way, naming `path` (exit 3; plan ruling C). The
        leaf's permissions are never changed once it exists.
    """
    path = resolve_data_dir(path)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise CliError(f"{path.parent} exists and is not a directory") from exc
    except PermissionError as exc:
        raise CliError(f"{path.parent} is not writable") from exc
    except OSError as exc:
        raise CliError(f"Can't prepare data directory {path}: {exc}", 3) from exc

    parent_mode = stat.S_IMODE(os.stat(path.parent).st_mode)
    # Only *other*-writable without the sticky bit is refused: a
    # group-writable parent is normal under a user-private-group umask
    # (0002, the Ubuntu default) and must not be rejected (plan ruling D).
    if os.name != "nt" and parent_mode & 0o002 and not parent_mode & stat.S_ISVTX:
        raise CliError(
            f"{path.parent} is writable by other users without the sticky bit; "
            f"refusing to create {path} inside it"
        )

    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileExistsError as exc:
        raise CliError(f"{path} exists and is not a directory") from exc
    except PermissionError as exc:
        raise CliError(f"{path.parent} is not writable") from exc
    except OSError as exc:
        raise CliError(f"Can't prepare data directory {path}: {exc}", 3) from exc

    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise CliError(f"{path} is a symlink; refusing to use it as the data directory")
    if os.name != "nt" and info.st_uid != os.getuid():
        raise CliError(
            f"{path} is not owned by the current user; refusing to use it as the data directory"
        )
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise CliError(f"{path} is accessible by other users; run: chmod 700 {path}")
    data_dir = DataDir(path)
    try:
        data_dir.lake.mkdir(exist_ok=True)
        write_marker(path)
    except OSError as exc:
        raise CliError(f"Can't prepare data directory {data_dir.lake}: {exc}", 3) from exc
    return data_dir


def _read_master_key(path: Path) -> str:
    """Open, check and validate an existing master key file.

    Opens the file once with `O_NOFOLLOW` (where the platform supports it)
    and checks ownership and permissions via `os.fstat` on that same file
    descriptor before reading from it, so there is no window between
    checking the file and reading it (TOCTOU) — the same pattern
    `lakana._config._read_profile_file` uses for the profile file.

    Parameters
    ----------
    path
        The master key file.

    Returns
    -------
    str
        The validated, base64-encoded 32-byte key text (stripped).

    Raises
    ------
    FileNotFoundError
        If the file doesn't exist.
    CliError
        If the path is a symlink (exit 2); if, on POSIX, it isn't owned by
        the current user or is accessible to group/other (exit 2); if its
        contents don't decode as exactly 32 bytes of base64, or aren't
        valid UTF-8 (exit 3, with a hint to run `lakana server reset --local` and
        rotate the key — the file itself is corrupt or wrong-mode); or if
        opening or reading it otherwise raises `OSError` for any other
        reason, e.g. the path is a directory, or a transient I/O error
        (exit 3, naming `path`, with **no** rotate advice — nothing here
        says the key itself is bad, so telling the caller to delete their
        secrets over what might be a transient `EIO`/`EACCES` would be
        wrong; plan ruling C).
    """
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_RDONLY | o_nofollow)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if o_nofollow and exc.errno == errno.ELOOP:
            raise CliError(f"{path} is a symlink; refusing to read it") from exc
        raise CliError(f"Can't read {path}: {exc}", 3) from exc
    try:
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if os.name != "nt" and info.st_uid != os.getuid():
                raise CliError(
                    f"{path} is not owned by the current user; stop Lakana and run "
                    f"'lakana server reset --local' to rotate the key"
                )
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise CliError(f"{path} is accessible by other users; run: chmod 600 {path}")
            text = handle.read().strip()
    except UnicodeDecodeError as exc:
        raise CliError(
            f"{path} is not a valid master key; stop Lakana and run "
            "'lakana server reset --local' to rotate it",
            3,
        ) from exc
    except OSError as exc:
        raise CliError(f"Can't read {path}: {exc}", 3) from exc
    try:
        key_bytes = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CliError(
            f"{path} is not a valid master key; stop Lakana and run "
            "'lakana server reset --local' to rotate it",
            3,
        ) from exc
    if len(key_bytes) != _MASTER_KEY_BYTES:
        raise CliError(
            f"{path} is not a valid master key; stop Lakana and run "
            "'lakana server reset --local' to rotate it",
            3,
        )
    return text


def load_or_create_master_key(data_dir: DataDir) -> str:
    """Return the secrets master key, generating it on first use.

    Parameters
    ----------
    data_dir
        The prepared data directory.

    Returns
    -------
    str
        A base64-encoded 32-byte key.

    Raises
    ------
    CliError
        See `_read_master_key` for the checks applied to an existing key.
        Also raised (exit 3, naming `path`) if creating a fresh key fails
        with an `OSError` at any step — writing the temp file, `os.link`
        (which fails with more than `FileExistsError` on filesystems
        without hard-link support, e.g. some FUSE, SMB or exFAT mounts),
        or the directory fsync (plan ruling C).

    Notes
    -----
    Creation writes the key to a fresh temp file (`O_CREAT | O_EXCL |
    O_NOFOLLOW`, mode 0600, random name), fsyncs it, and then links it into
    place with `os.link`, which fails atomically with `FileExistsError` if
    another process's key is already there — the loser reads that key back
    rather than overwriting it. The temp file is removed either way, and the
    directory is fsynced after a successful link so the new entry survives a
    crash.
    """
    path = data_dir.master_key_path
    try:
        return _read_master_key(path)
    except FileNotFoundError:
        pass

    try:
        key = base64.b64encode(secrets.token_bytes(_MASTER_KEY_BYTES)).decode("ascii")
        temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(key + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                return _read_master_key(path)
        finally:
            temp.unlink(missing_ok=True)

        _fsync_directory(path.parent)
        return key
    except OSError as exc:
        raise CliError(f"Can't create master key {path}: {exc}", 3) from exc


def _fsync_directory(path: Path) -> None:
    """Fsync a directory so a new entry within it survives a crash.

    A no-op on Windows, which has no equivalent operation.

    Parameters
    ----------
    path
        The directory to fsync.
    """
    if os.name == "nt":  # pragma: no cover — Windows only; see plan ruling 4
        return
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
