"""The sweep of attempt directories a dead worker left behind."""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from pathlib import Path

import pytest

from lakana_worker.attempt_sweep import AttemptSweeper

_NOW = 1_000_000.0
_MAX_AGE = 1000.0


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """Provide a private `attempts` directory.

    Parameters
    ----------
    tmp_path
        Pytest's temporary directory.

    Returns
    -------
    Path
        The directory, mode 0700.
    """
    path = tmp_path / "attempts"
    path.mkdir(mode=0o700)
    return path


def _sweeper(work: Path, *, interval: float = 3600.0, now: float = _NOW) -> AttemptSweeper:
    """Build a sweeper with a frozen clock.

    Parameters
    ----------
    work
        The attempts directory.
    interval
        Seconds between sweeps.
    now
        The wall-clock time.

    Returns
    -------
    AttemptSweeper
        The sweeper.
    """
    return AttemptSweeper(work, max_age=_MAX_AGE, interval=interval, clock=lambda: now)


def _attempt(work: Path, name: str, age: float) -> Path:
    """Create an attempt directory with a file in it and a given age.

    Parameters
    ----------
    work
        The attempts directory.
    name
        The directory name.
    age
        Seconds before `_NOW` its mtime is set to.

    Returns
    -------
    Path
        The directory.
    """
    path = work / name
    path.mkdir(mode=0o700)
    (path / "result.json").write_text("x")
    os.utime(path, (_NOW - age, _NOW - age))
    return path


def test_it_removes_an_old_orphan_and_keeps_a_young_one(work: Path) -> None:
    old = _attempt(work, "attempt-abcd1234", _MAX_AGE + 1)
    young = _attempt(work, "attempt-wxyz_789", _MAX_AGE - 1)
    assert _sweeper(work).sweep() == 1
    assert not old.exists()
    assert young.exists()


def test_it_removes_an_old_connection_check_directory_too(work: Path) -> None:
    """A connection check's directory (``attempt-check_`` and mkdtemp's eight) is an attempt."""
    old = _attempt(work, "attempt-check_ab12cd_3", _MAX_AGE + 1)
    young = _attempt(work, "attempt-check_wxyz7890", _MAX_AGE - 1)
    assert _sweeper(work).sweep() == 1
    assert not old.exists()
    assert young.exists()


def test_it_keeps_an_old_directory_a_running_task_of_this_worker_holds(work: Path) -> None:
    held = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    sweeper = _sweeper(work)
    sweeper.hold(held)
    assert sweeper.sweep() == 0
    assert held.exists()
    sweeper.release(held)
    sweeper.release(held)
    assert sweeper.sweep() == 1
    assert not held.exists()


def test_it_never_follows_a_symlink_named_like_an_attempt(work: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    os.utime(outside, (_NOW - _MAX_AGE * 5, _NOW - _MAX_AGE * 5))
    link = work / "attempt-abcd1234"
    link.symlink_to(outside)
    assert _sweeper(work).sweep() == 0
    assert link.is_symlink()
    assert (outside / "precious").read_text() == "keep"


def test_it_leaves_foreign_names_alone_and_logs_each_once(
    work: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for name in (
        "venvs",
        "attempt-short",
        "attempt-abcd1234-extra",
        "attempt-check_short",
        "notes.txt",
    ):
        entry = work / name
        entry.mkdir(mode=0o700) if name != "notes.txt" else entry.write_text("x")
        os.utime(entry, (_NOW - _MAX_AGE * 5, _NOW - _MAX_AGE * 5))
    sweeper = _sweeper(work)
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep() == 0
        assert sweeper.sweep() == 0
    assert sorted(p.name for p in work.iterdir()) == [
        "attempt-abcd1234-extra",
        "attempt-check_short",
        "attempt-short",
        "notes.txt",
        "venvs",
    ]
    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if "venvs" in m]) == 1
    assert len(messages) == 5


def test_it_leaves_an_old_file_with_an_attempt_name_alone(work: Path) -> None:
    file = work / "attempt-abcd1234"
    file.write_text("x")
    os.utime(file, (_NOW - _MAX_AGE * 5, _NOW - _MAX_AGE * 5))
    assert _sweeper(work).sweep() == 0
    assert file.exists()


def test_it_survives_a_directory_vanishing_mid_sweep(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vanishing = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    other = _attempt(work, "attempt-efgh5678", _MAX_AGE * 5)
    real = os.lstat

    def lstat(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        if Path(path) == vanishing:
            raise FileNotFoundError(path)
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", lstat)
    assert _sweeper(work).sweep() == 1
    assert vanishing.exists() and not other.exists()


def test_it_survives_an_entry_it_cannot_remove(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stuck = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    monkeypatch.setattr("lakana_worker.attempt_sweep.remove_private_tree", lambda path: False)
    assert _sweeper(work).sweep() == 0
    assert stuck.exists()


def test_it_does_nothing_when_the_directory_is_missing_or_not_private(
    work: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert _sweeper(tmp_path / "missing").sweep() == 0
    old = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    work.chmod(0o755)
    with caplog.at_level(logging.WARNING):
        assert _sweeper(work).sweep() == 0
    assert old.exists()
    assert "not private" in caplog.text
    link = tmp_path / "linked"
    link.symlink_to(work)
    work.chmod(0o700)
    assert _sweeper(link).sweep() == 0
    assert old.exists()


def test_it_survives_an_unreadable_directory_listing(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(path: object) -> list[str]:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "listdir", broken)
    with caplog.at_level(logging.WARNING):
        assert _sweeper(work).sweep() == 0
    assert "could not be listed" in caplog.text


def test_maybe_sweep_runs_at_once_then_at_most_once_per_interval(work: Path) -> None:
    ticks = iter([0.0, 100.0, 3599.0, 3600.0])
    sweeper = AttemptSweeper(
        work, max_age=_MAX_AGE, interval=3600.0, clock=lambda: _NOW, monotonic=lambda: next(ticks)
    )
    swept = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    sweeper.maybe_sweep()
    assert not swept.exists()
    for name in ("attempt-efgh5678", "attempt-ijkl9012"):
        _attempt(work, name, _MAX_AGE * 5)
        sweeper.maybe_sweep()
        assert (work / name).exists()
    sweeper.maybe_sweep()
    assert not (work / "attempt-efgh5678").exists()


def test_an_interval_of_zero_disables_the_sweep(work: Path) -> None:
    old = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    _sweeper(work, interval=0).maybe_sweep()
    assert old.exists()


def test_maybe_sweep_with_nothing_to_remove_just_records_the_time(work: Path) -> None:
    sweeper = _sweeper(work)
    sweeper.maybe_sweep()
    sweeper.maybe_sweep()


def _lock_elsewhere(path: Path, mode: int) -> int:
    """Lock a directory through a descriptor of its own, as another worker would.

    Parameters
    ----------
    path
        The directory.
    mode
        `fcntl.LOCK_SH` or `fcntl.LOCK_EX`.

    Returns
    -------
    int
        The descriptor; closing it releases the lock.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(descriptor, mode)
    return descriptor


def test_an_old_directory_another_worker_holds_a_shared_lock_on_is_kept(work: Path) -> None:
    """Review R5: the lock, not the clock, protects a live attempt of another worker."""
    live = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    other_worker = _lock_elsewhere(live, fcntl.LOCK_SH)
    try:
        assert _sweeper(work).sweep() == 0
        assert live.exists()
    finally:
        os.close(other_worker)
    assert _sweeper(work).sweep() == 1
    assert not live.exists()


def test_a_young_unlocked_directory_is_kept(work: Path) -> None:
    """Age is the second guard: it also covers the instant before the worker locks."""
    young = _attempt(work, "attempt-abcd1234", 1.0)
    assert _sweeper(work).sweep() == 0
    assert young.exists()


def test_hold_locks_the_directory_until_release_and_does_not_leak_descriptors(
    work: Path,
) -> None:
    attempt = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    sweeper = _sweeper(work)
    sweeper.hold(attempt)
    assert sweeper.holding() == 1
    probe = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sweeper.release(attempt)
        sweeper.release(attempt)
        assert sweeper.holding() == 0
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


def test_the_held_descriptor_is_not_inherited_by_a_child_process(work: Path) -> None:
    attempt = _attempt(work, "attempt-abcd1234", 1.0)
    sweeper = _sweeper(work)
    sweeper.hold(attempt)
    try:
        [descriptor] = sweeper._held.values()
        assert not os.get_inheritable(descriptor)
    finally:
        sweeper.release(attempt)


def _skip_as_root() -> None:
    """Skip a test that needs a mode to keep its owner out, which root ignores."""
    if os.geteuid() == 0:
        pytest.skip("root opens any directory")


def test_an_old_directory_user_code_set_to_mode_zero_is_removed(work: Path) -> None:
    """The sweeper restores access without following a link, then removes it like any orphan."""
    _skip_as_root()
    locked = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    (locked / "sub").mkdir(mode=0o700)
    (locked / "sub" / "f").write_text("x")
    os.utime(locked, (_NOW - _MAX_AGE * 5, _NOW - _MAX_AGE * 5))
    locked.chmod(0)
    try:
        assert _sweeper(work).sweep() == 1
        assert not locked.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)


def test_a_young_directory_of_mode_zero_is_not_touched(work: Path) -> None:
    """The age guard comes first; nothing younger than `max_age` is chmod-ed."""
    _skip_as_root()
    young = _attempt(work, "attempt-abcd1234", 1.0)
    young.chmod(0)
    try:
        assert _sweeper(work).sweep() == 0
        assert young.stat().st_mode & 0o777 == 0
    finally:
        young.chmod(0o700)


def test_a_mode_zero_directory_a_live_worker_holds_is_kept_but_made_accessible_again(
    work: Path,
) -> None:
    """The lock still decides after the restore; a held directory stays.

    The live task runs as the same user and its descriptor stays valid, so the only
    thing it can see is its directory's mode returning to 0700.
    """
    _skip_as_root()
    live = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    holder = _sweeper(work)
    holder.hold(live)
    live.chmod(0)
    try:
        assert _sweeper(work).sweep() == 0
        assert live.exists()
        assert (live / "result.json").read_text() == "x"
        assert live.stat().st_mode & 0o777 == 0o700
    finally:
        holder.release(live)
    assert _sweeper(work).sweep() == 1


def test_a_directory_whose_access_cannot_be_restored_is_left_alone_and_logged(
    work: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_as_root()
    locked = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    locked.chmod(0)

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(args[0])

    monkeypatch.setattr(os, "chmod", refuse)
    try:
        with caplog.at_level(logging.WARNING):
            assert _sweeper(work).sweep() == 0
    finally:
        monkeypatch.undo()
        locked.chmod(0o700)
    assert locked.exists()
    assert "could not be opened" in caplog.text


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_a_directory_swapped_for_a_link_before_the_restore_is_not_chmoded(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    """The restore uses `O_NOFOLLOW` and refuses a non-directory; a link is not followed."""
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    swapped = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.open
    calls: list[str] = []

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == swapped and not calls:
            calls.append("first")
            swapped.rename(tmp_path / "moved")
            swapped.symlink_to(target)
            raise error(13, "denied")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    assert _sweeper(work).sweep() == 0
    assert target.stat().st_mode & 0o777 == 0o755
    assert swapped.is_symlink()


def test_a_directory_that_vanishes_during_the_restore_is_skipped(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_as_root()
    gone = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    gone.chmod(0)

    def vanish(name: str, parent_fd: int, path: Path) -> bool:
        raise FileNotFoundError(name)

    monkeypatch.setattr("lakana_worker.attempt_sweep.restore_directory_access", vanish)
    try:
        assert _sweeper(work).sweep() == 0
    finally:
        monkeypatch.undo()
        gone.chmod(0o700)


def test_a_directory_whose_restore_handle_cannot_be_opened_is_left_alone_and_logged(
    work: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If even the restore's own open is refused, the warning stays."""
    refused = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.open

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path).name == refused.name:  # by path, or by name below the work directory
            raise PermissionError(13, "denied")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    with caplog.at_level(logging.WARNING):
        assert _sweeper(work).sweep() == 0
    assert "could not be opened" in caplog.text


def test_a_directory_that_fails_to_open_for_another_reason_is_left_alone_and_logged(
    work: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.open

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == broken:
            raise OSError(5, "input/output error")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    with caplog.at_level(logging.WARNING):
        assert _sweeper(work).sweep() == 0
    assert "could not be opened" in caplog.text


def test_a_directory_that_vanishes_before_it_is_opened_is_skipped(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.open

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == gone:
            raise FileNotFoundError(path)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    assert _sweeper(work).sweep() == 0


def test_hold_closes_its_descriptor_when_the_lock_cannot_be_taken(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(work, "attempt-abcd1234", 1.0)

    def refuse(descriptor: int, operation: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(fcntl, "flock", refuse)
    sweeper = _sweeper(work)
    with pytest.raises(OSError, match="Input/output"):
        sweeper.hold(attempt)
    assert sweeper.holding() == 0


def test_a_sweeper_that_locked_an_orphaned_inode_does_not_remove_the_new_directory(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 opened D, S2 removed it, a worker reused the name.

    S1's lock succeeds on the orphaned inode; it must notice the path is another
    directory now and leave the live one alone.
    """
    old = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    reached, resume = threading.Event(), threading.Event()
    paused: list[bool] = []
    real = fcntl.flock

    def flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "S1"
            and operation == fcntl.LOCK_EX | fcntl.LOCK_NB
            and not paused
        ):
            paused.append(True)
            reached.set()
            resume.wait(10)
        real(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", flock)
    s1 = threading.Thread(target=_sweeper(work).sweep, name="S1")
    s1.start()
    assert reached.wait(10)
    assert _sweeper(work).sweep() == 1
    assert not old.exists()
    live = work / old.name
    live.mkdir(mode=0o700)
    (live / "result.json").write_text("live")
    worker = _sweeper(work)
    worker.hold(live)
    resume.set()
    s1.join(10)
    try:
        assert (live / "result.json").read_text() == "live"
    finally:
        worker.release(live)


def test_a_busy_lock_is_skipped_silently_but_any_other_lock_error_is_logged_once(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    second = _attempt(work, "attempt-efgh5678", _MAX_AGE * 5)
    error: list[OSError] = [BlockingIOError(11, "busy")]

    def flock(descriptor: int, operation: int) -> None:
        raise error[0]

    monkeypatch.setattr(fcntl, "flock", flock)
    sweeper = _sweeper(work)
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep() == 0
        assert caplog.records == []
        error[0] = OSError(95, "Operation not supported")
        assert sweeper.sweep() == 0
        assert sweeper.sweep() == 0
    assert first.exists() and second.exists()
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "attempt-" in message and "Operation not supported" in message


def test_a_directory_that_vanishes_after_it_was_locked_is_skipped(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.lstat
    calls: list[int] = []

    def lstat(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> os.stat_result:
        if Path(path) == gone:
            calls.append(1)
            if len(calls) == 2:
                raise FileNotFoundError(path)
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", lstat)
    assert _sweeper(work).sweep() == 0
    assert gone.exists()


def test_a_foreign_entry_with_a_hostile_name_is_logged_escaped(
    work: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file name with a line break cannot forge a log record."""
    (work / "x\n2026-10-08 ERROR forged record\x1b[2J").write_text("x")
    with caplog.at_level(logging.WARNING):
        _sweeper(work).sweep()
    [record] = caplog.records
    message = record.getMessage()
    assert "\n" not in message and "\x1b" not in message
    assert "x\\n2026-10-08 ERROR forged record\\x1b[2J" in message


def test_a_directory_swapped_for_a_young_one_before_it_is_opened_is_kept(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age rule is decided on what was opened, not on the path looked at before."""
    old = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.open
    swapped: list[bool] = []

    def swap_then_open(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == old and not swapped:
            swapped.append(True)
            old.rename(work / "moved-away")
            old.mkdir(mode=0o700)
            (old / "result.json").write_text("live")
            os.utime(old, (_NOW - 1, _NOW - 1))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    assert _sweeper(work).sweep() == 0
    assert (old / "result.json").read_text() == "live"


@pytest.mark.usefixtures("without_o_path")
def test_without_o_path_an_old_directory_of_mode_zero_is_removed(work: Path) -> None:
    """Without `O_PATH` (macOS) the restore is a `chmod` relative to the work directory."""
    _skip_as_root()
    locked = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    locked.chmod(0)
    try:
        assert _sweeper(work).sweep() == 1
        assert not locked.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)


@pytest.mark.usefixtures("without_o_path")
def test_without_o_path_a_directory_swapped_for_a_link_before_the_restore_is_not_chmoded(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race as with `O_PATH`: the restore's `lstat` sees the link and stops."""
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    swapped = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    real = os.open

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == swapped and swapped.is_dir() and not swapped.is_symlink():
            swapped.rename(tmp_path / "moved")
            swapped.symlink_to(target)
            raise PermissionError(13, "denied")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    assert _sweeper(work).sweep() == 0
    assert target.stat().st_mode & 0o777 == 0o755
    assert swapped.is_symlink()


def test_a_work_directory_that_cannot_be_opened_for_the_restore_leaves_the_attempt_alone(
    work: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore works below a descriptor of the work directory; without one, no `chmod`."""
    _skip_as_root()
    locked = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    locked.chmod(0)
    real = os.open

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == work:
            raise OSError(5, "input/output error")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    try:
        with caplog.at_level(logging.WARNING):
            assert _sweeper(work).sweep() == 0
        assert locked.stat().st_mode & 0o777 == 0
    finally:
        monkeypatch.undo()
        locked.chmod(0o700)
    assert "could not be opened" in caplog.text


def test_a_restored_directory_that_cannot_be_reopened_is_left_alone_and_logged(
    work: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_as_root()
    locked = _attempt(work, "attempt-abcd1234", _MAX_AGE * 5)
    locked.chmod(0)
    real = os.open

    def opening(path: str | os.PathLike[str], flags: int, *args: int, **kwargs: int) -> int:
        if kwargs.get("dir_fd") is not None and flags & os.O_DIRECTORY:
            raise OSError(5, "input/output error")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    try:
        with caplog.at_level(logging.WARNING):
            assert _sweeper(work).sweep() == 0
    finally:
        monkeypatch.undo()
    assert locked.stat().st_mode & 0o777 == 0o700
    assert "could not be opened" in caplog.text
