"""The sweep of temporary ``.part`` files a crash left under a local storage root (#614)."""

from __future__ import annotations

import errno
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from lakana_core.models import Environment
from lakana_core.storage import temp_sweep
from lakana_core.storage.fsspec_storage import FsspecStorage, _LocalStreamWriter, is_temp_name
from lakana_core.storage.resolver import StorageResolver
from lakana_core.storage.temp_sweep import TEMP_FILE_MIN_AGE, TempSweepReport, sweep_temp_files

_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
_STALE = TEMP_FILE_MIN_AGE.total_seconds() + 60
_FRESH = TEMP_FILE_MIN_AGE.total_seconds() - 60
_HEX = "0123456789abcdef"


def _age(path: Path, seconds: float) -> None:
    """Set a path's own modification time to `seconds` before `_NOW`, never through a link.

    Parameters
    ----------
    path
        The file, directory or symbolic link.
    seconds
        How old it is to look.
    """
    moment = (_NOW - timedelta(seconds=seconds)).timestamp()
    os.utime(path, (moment, moment), follow_symlinks=False)


class World:
    """Environments whose storage roots are directories, and a way to write into them.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Directory under which the storage roots are made.
    """

    def __init__(self, session: Session, tmp_path: Path) -> None:
        self.session = session
        self.tmp_path = tmp_path

    def environment(self, name: str, root: str = "store") -> Path:
        """Create an environment whose storage root is a directory.

        Parameters
        ----------
        name
            The environment's name.
        root
            The directory's name; environments with the same name share a root.

        Returns
        -------
        Path
            The root directory (made).
        """
        directory = self.tmp_path / root
        directory.mkdir(exist_ok=True)
        self.session.add(
            Environment(id=uuid.uuid4(), name=name, storage_root=f"file://{directory}")
        )
        self.session.commit()
        return directory

    def crashed_write(self, env: str, path: str, *, age: float) -> Path:
        """Leave the temporary file of a write that never committed, as a killed process does.

        Parameters
        ----------
        env
            The environment's name.
        path
            Root-relative path of the object being written.
        age
            Seconds before `_NOW` its modification time is set to.

        Returns
        -------
        Path
            The temporary file on disk.
        """
        storage = StorageResolver(self.session).for_environment(env).storage
        stream = storage.open_write(path)
        assert isinstance(stream, _LocalStreamWriter)
        stream.write(b"half")
        temp = Path(stream._temp)  # the name the writer chose
        assert stream._handle is not None
        stream._handle.close()  # the process died: no commit, no abort
        _age(temp, age)
        return temp

    def sweep(self, **kwargs: Any) -> TempSweepReport:  # ruff: ignore[any-type]
        """Run the sweep at `_NOW`.

        Parameters
        ----------
        **kwargs
            Passed on to `sweep_temp_files`.

        Returns
        -------
        TempSweepReport
            What it did.
        """
        return sweep_temp_files(
            self.session,
            StorageResolver(self.session),
            now=_NOW,
            **kwargs,
        )


@pytest.fixture
def world(session: Session, tmp_path: Path) -> World:
    """Provide the scene builder.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Pytest's temporary directory.

    Returns
    -------
    World
        The scene builder.
    """
    return World(session, tmp_path)


def test_a_stale_temporary_file_of_a_crashed_write_is_removed(world: World) -> None:
    root = world.environment("dev")
    (root / "_logs").mkdir()
    (root / "_logs" / "task.log").write_bytes(b"the object")
    temp = world.crashed_write("dev", "_logs/task.log", age=_STALE)
    nested = world.crashed_write("dev", "warehouse/gold/sales/data/f.parquet", age=_STALE)
    assert temp.name.startswith(".task.log.") and temp.name.endswith(".part")
    report = world.sweep()
    assert not temp.exists() and not nested.exists()
    assert (root / "_logs" / "task.log").read_bytes() == b"the object"
    assert report.removed == 2
    assert report.freed_bytes == 8
    assert report.stopped_early is False


def test_a_fresh_temporary_file_is_kept(world: World) -> None:
    world.environment("dev")
    temp = world.crashed_write("dev", "_logs/task.log", age=_FRESH)
    report = world.sweep()
    assert temp.exists()
    assert (report.removed, report.kept_fresh) == (0, 1)


def test_the_age_is_the_one_the_caller_gives(world: World) -> None:
    world.environment("dev")
    temp = world.crashed_write("dev", "_logs/task.log", age=_STALE)
    assert world.sweep(min_age=timedelta(seconds=_STALE + 60)).removed == 0
    assert temp.exists()
    assert world.sweep(min_age=timedelta(seconds=_STALE - 60)).removed == 1
    assert not temp.exists()


def test_the_age_is_never_less_than_an_hour(world: World) -> None:
    world.environment("dev")
    temp = world.crashed_write("dev", "_logs/task.log", age=_FRESH)
    assert world.sweep(min_age=timedelta(seconds=1)).removed == 0
    assert temp.exists()


@pytest.mark.parametrize(
    "name",
    [
        ".notes.part",  # no random part
        f"task.log.{_HEX}.part",  # no leading dot
        f".task.log.{_HEX.upper()}.part",  # not the writer's lower-case hex
        f".task.log.{_HEX[:-1]}.part",  # one hex digit short
        f".task.log.{_HEX}.partial",
        f".task.log.{_HEX}.part.bak",
        f".{_HEX}.part",  # no object name before the random part
        "data.parquet",
        ".dropped",
    ],
)
def test_an_old_file_that_only_looks_like_a_temporary_file_is_kept(world: World, name: str) -> None:
    root = world.environment("dev")
    (root / "artifacts").mkdir()
    file = root / "artifacts" / name
    file.write_bytes(b"keep me")
    _age(file, _STALE * 100)
    report = world.sweep()
    assert file.read_bytes() == b"keep me"
    assert report.removed == 0


def test_an_old_directory_named_like_a_temporary_file_is_left_alone(world: World) -> None:
    root = world.environment("dev")
    directory = root / f".task.log.{_HEX}.part"
    directory.mkdir()
    (directory / "inside").write_bytes(b"x")
    _age(directory, _STALE)
    report = world.sweep()
    assert (directory / "inside").read_bytes() == b"x"
    assert (report.removed, report.left_alone) == (0, 1)


def test_a_stale_link_named_like_a_temporary_file_is_removed_and_never_followed(
    world: World, tmp_path: Path
) -> None:
    root = world.environment("dev")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "precious"
    target.write_bytes(b"not the platform's")
    elsewhere = outside / "tree"
    elsewhere.mkdir()
    stale_there = elsewhere / f".x.{_HEX}.part"
    stale_there.write_bytes(b"not the platform's either")
    _age(stale_there, _STALE)
    to_file = root / f".a.{_HEX}.part"
    to_file.symlink_to(target)
    to_directory = root / f".b.{_HEX}.part"
    to_directory.symlink_to(elsewhere, target_is_directory=True)
    _age(to_file, _STALE)
    _age(to_directory, _STALE)
    _age(target, 0)  # the target's own time must not decide anything
    report = world.sweep()
    assert not os.path.lexists(to_file) and not os.path.lexists(to_directory)
    assert target.read_bytes() == b"not the platform's"
    assert stale_there.read_bytes() == b"not the platform's either"
    assert report.removed == 2
    assert report.freed_bytes == 0  # a link frees nothing of what it points to


def test_a_fresh_link_to_an_old_file_is_kept(world: World, tmp_path: Path) -> None:
    root = world.environment("dev")
    target = tmp_path / "old"
    target.write_bytes(b"x")
    _age(target, _STALE * 10)
    link = root / f".a.{_HEX}.part"
    link.symlink_to(target)
    _age(link, _FRESH)
    assert world.sweep().removed == 0
    assert os.path.islink(link)


def test_a_linked_directory_is_not_walked(world: World, tmp_path: Path) -> None:
    root = world.environment("dev")
    outside = tmp_path / "outside"
    outside.mkdir()
    stale = outside / f".x.{_HEX}.part"
    stale.write_bytes(b"x")
    _age(stale, _STALE)
    (root / "warehouse").symlink_to(outside, target_is_directory=True)
    assert world.sweep().removed == 0
    assert stale.exists()


def test_a_storage_root_reached_through_a_link_is_swept(world: World, tmp_path: Path) -> None:
    """The operator may point the root at a link (``/data`` to a mounted disk); below it, no."""
    disk = tmp_path / "disk"
    disk.mkdir()
    (tmp_path / "store").symlink_to(disk, target_is_directory=True)
    world.environment("dev", root="store")
    temp = world.crashed_write("dev", "_logs/task.log", age=_STALE)
    assert temp.resolve().parent.parent == disk.resolve()
    assert world.sweep().removed == 1
    assert not temp.exists()


def test_environments_sharing_a_root_sweep_it_once(world: World) -> None:
    world.environment("dev")
    world.environment("prod")
    world.crashed_write("dev", "a", age=_STALE)
    report = world.sweep()
    assert report.removed == 1
    assert report.failed == 0


def test_a_root_nothing_was_written_to_yet_is_no_error(world: World, tmp_path: Path) -> None:
    world.session.add(
        Environment(id=uuid.uuid4(), name="new", storage_root=f"file://{tmp_path / 'none'}")
    )
    world.session.commit()
    report = world.sweep()
    assert (report.removed, report.failed_roots) == (0, 0)


def test_a_remote_root_is_skipped_without_a_listing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On S3 a write uploads parts to its final key: there is no temporary file to look for."""
    world.session.add(Environment(id=uuid.uuid4(), name="s3", storage_root="s3://bucket/x"))
    world.session.commit()
    walked: list[str] = []
    monkeypatch.setattr(os, "fwalk", lambda *a, **k: walked.append("walked") or iter(()))
    report = world.sweep()
    assert walked == []
    assert (report.removed, report.failed_roots) == (0, 0)


def test_a_failing_delete_is_counted_and_the_others_go_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev")
    first = world.crashed_write("dev", "a", age=_STALE)
    second = world.crashed_write("dev", "b", age=_STALE)
    real = os.unlink
    refused = first.name

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == refused:
            raise PermissionError(errno.EACCES, "Permission denied")
        real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    report = world.sweep()
    assert (report.removed, report.failed) == (1, 1)
    assert first.exists() and not second.exists()


def test_a_file_gone_before_its_delete_is_not_a_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("dev")
    temp = world.crashed_write("dev", "a", age=_STALE)
    real = os.unlink

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        real(path, dir_fd=dir_fd)
        raise FileNotFoundError(errno.ENOENT, "gone")

    monkeypatch.setattr(os, "unlink", unlink)
    report = world.sweep()
    assert (report.removed, report.failed) == (0, 0)
    assert not temp.exists()


def test_a_root_whose_walk_fails_is_counted_and_the_next_root_is_swept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.environment("a", root="one")
    world.environment("b", root="two")
    world.crashed_write("a", "x", age=_STALE)
    world.crashed_write("b", "x", age=_STALE)
    real = FsspecStorage.remove_stale_temp_files
    calls: list[int] = []

    def failing_first(self: FsspecStorage, **kwargs: Any) -> object:  # ruff: ignore[any-type]
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk gone")
        return real(self, **kwargs)

    monkeypatch.setattr(FsspecStorage, "remove_stale_temp_files", failing_first)
    report = world.sweep()
    assert (report.removed, report.failed_roots) == (1, 1)


def test_a_stop_ends_the_run_before_the_next_decision(world: World) -> None:
    world.environment("dev")
    temp = world.crashed_write("dev", "a", age=_STALE)
    report = world.sweep(should_stop=lambda: True)
    assert report.stopped_early is True
    assert report.removed == 0
    assert temp.exists()


def test_the_deadline_ends_the_run_inside_a_root(world: World) -> None:
    world.environment("dev")
    for name in ("a", "b", "c"):
        world.crashed_write("dev", f"d/{name}", age=_STALE)
    ticks = iter([0.0, 0.0, 0.0, 0.0, 100.0])

    def clock() -> float:
        return next(ticks, 100.0)

    report = world.sweep(max_seconds=10.0, monotonic=clock)
    assert report.stopped_early is True
    assert report.removed < 3


def test_the_rotation_changes_which_top_level_directory_comes_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = world.environment("dev")
    for name in ("a", "b", "c"):
        (root / name).mkdir()
    seen: list[list[str]] = []
    real = os.fwalk

    def recording(*args: Any, **kwargs: Any) -> object:  # ruff: ignore[any-type]
        order: list[str] = []
        seen.append(order)
        for entry in real(*args, **kwargs):
            yield entry
            if entry[0] != str(root.resolve()):
                order.append(os.path.basename(entry[0]))

    monkeypatch.setattr(os, "fwalk", recording)
    world.sweep(rotation=0)
    world.sweep(rotation=1)
    assert seen[0] == ["a", "b", "c"]
    assert seen[1] == ["b", "c", "a"]


def test_the_sweep_logs_what_it_removed(world: World, caplog: pytest.LogCaptureFixture) -> None:
    world.environment("dev")
    world.crashed_write("dev", "a", age=_STALE)
    with caplog.at_level("INFO", logger=temp_sweep.__name__):
        world.sweep()
    assert "Temporary file sweep: 1 leftover temporary file(s) removed (4 bytes)" in caplog.text


def test_a_quiet_run_logs_nothing(world: World, caplog: pytest.LogCaptureFixture) -> None:
    world.environment("dev")
    with caplog.at_level("INFO", logger=temp_sweep.__name__):
        world.sweep()
    assert caplog.text == ""


def test_the_writer_names_its_temporary_file_the_way_the_sweep_recognises_it(
    tmp_path: Path,
) -> None:
    """The two sides of one convention: a long or odd object name still gives a swept name."""
    storage = FsspecStorage(f"file://{tmp_path}", {})
    for name in ("x", "n" * 250, "ü.parquet", ".hidden"):
        stream = storage.open_write(name)
        assert isinstance(stream, _LocalStreamWriter)
        temp = os.path.basename(stream._temp)
        stream.abort()
        assert is_temp_name(temp), temp


def test_a_stop_before_the_first_directory_walks_nothing(tmp_path: Path) -> None:
    storage = FsspecStorage(f"file://{tmp_path}", {})
    stale = tmp_path / f".a.{_HEX}.part"
    stale.write_bytes(b"x")
    _age(stale, _STALE)
    counts = storage.remove_stale_temp_files(older_than=_NOW, should_stop=lambda: True)
    assert counts.stopped is True
    assert counts.removed == 0
    assert stale.exists()


def test_a_temporary_file_gone_before_it_is_looked_at_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer's commit or abort can take the file between the listing and the look."""
    storage = FsspecStorage(f"file://{tmp_path}", {})
    stale = tmp_path / f".a.{_HEX}.part"
    stale.write_bytes(b"x")
    _age(stale, _STALE)
    real = os.stat

    def vanishing(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:  # ruff: ignore[any-type]
        if path == stale.name and kwargs.get("dir_fd") is not None:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", vanishing)
    counts = storage.remove_stale_temp_files(older_than=_NOW, should_stop=lambda: False)
    assert (counts.removed, counts.failed, counts.left_alone, counts.kept_fresh) == (0, 0, 0, 0)


def test_a_backend_that_is_not_the_fsspec_one_is_skipped(world: World) -> None:
    """`Storage` is a protocol: a root resolved to another backend has no temporary files."""
    world.environment("dev")

    class _Other:
        """A resolver that hands out a storage of another kind."""

        def for_environment(self, name: str) -> object:
            """Resolve an environment.

            Parameters
            ----------
            name
                The environment.

            Returns
            -------
            object
                Something with a `storage` that is not an `FsspecStorage`.
            """
            return type("Resolved", (), {"storage": object()})()

    report = sweep_temp_files(
        world.session,
        _Other(),  # ty: ignore[invalid-argument-type]
        now=_NOW,
    )
    assert (report.removed, report.failed_roots) == (0, 0)
