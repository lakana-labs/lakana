"""Static checks on the repo's Dockerfile.

Non-root, healthcheck, no build tools in the final stage, a real home for
DuckDB's extension cache, tini as PID 1, and locked third-party versions.
"""

import re
import shlex
from pathlib import Path

import yaml

_DOCKERFILE = Path(__file__).resolve().parents[3] / "Dockerfile"
_COMPOSE = (
    Path(__file__).resolve().parents[1] / "src" / "lakana" / "cli" / "deploy" / "compose.yaml"
)


def _all_instructions() -> list[str]:
    r"""Return every logical instruction in the Dockerfile, builder stage included.

    Continuation lines (a physical line ending in `\\`) are joined onto the
    instruction they continue, so a check does not depend on which physical
    line within an instruction happens to carry the text it looks for.

    Returns
    -------
    list of str
        One joined, stripped, non-comment string per Dockerfile instruction.
    """
    raw = [ln.strip() for ln in _DOCKERFILE.read_text().splitlines()]
    raw = [ln for ln in raw if ln and not ln.startswith("#")]
    logical: list[str] = []
    buffer = ""
    for ln in raw:
        buffer = f"{buffer} {ln}".strip()
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
        else:
            logical.append(buffer)
            buffer = ""
    if buffer:
        logical.append(buffer)
    return logical


def _final_stage() -> list[str]:
    """Return the logical instructions after the last `FROM`.

    Returns
    -------
    list of str
        The final build stage's instructions, joined per `_all_instructions`.
    """
    instructions = _all_instructions()
    last_from = max(i for i, ln in enumerate(instructions) if ln.upper().startswith("FROM "))
    return instructions[last_from:]


def test_final_stage_runs_as_non_root_uid_10001() -> None:
    """The image runs as `lakana` (uid 10001), never root."""
    users = [ln for ln in _final_stage() if ln.upper().startswith("USER ")]
    assert users and users[-1].split()[1] in {"lakana", "10001", "10001:10001"}


def test_final_stage_has_a_healthz_healthcheck() -> None:
    """Docker and Compose can probe the server without curl."""
    assert any(ln.upper().startswith("HEALTHCHECK") and "/healthz" in ln for ln in _final_stage())


def test_final_stage_sets_a_writable_tmpdir() -> None:
    """DuckDB spills to TMPDIR, a directory created and owned by `lakana`."""
    lines = _final_stage()
    assert any(ln.upper().startswith("ENV ") and "TMPDIR=/var/tmp/lakana" in ln for ln in lines)
    assert any(
        "mkdir -p" in ln and "/var/tmp/lakana" in ln and "chown -R lakana:lakana" in ln
        for ln in lines
    )


def test_final_stage_sets_a_real_home_owned_by_lakana() -> None:
    """`HOME` must exist and be writable, or every worker query fails on `INSTALL httpfs`."""
    lines = _final_stage()
    assert any(ln.upper().startswith("ENV ") and "HOME=/home/lakana" in ln for ln in lines)
    assert any(
        "mkdir -p" in ln and "/home/lakana" in ln and "chown -R lakana:lakana" in ln for ln in lines
    )


def test_final_stage_pre_installs_httpfs_offline() -> None:
    """`httpfs` is installed at build time so no worker query hits the network for it."""
    assert any("INSTALL httpfs" in ln for ln in _final_stage())


def test_final_stage_uses_tini_as_entrypoint() -> None:
    """PID 1 must forward SIGTERM so `docker stop` and Kubernetes shutdown are prompt."""
    entrypoints = [ln for ln in _final_stage() if ln.upper().startswith("ENTRYPOINT")]
    assert entrypoints and "tini" in entrypoints[-1]


def test_builder_installs_third_party_packages_by_hash_from_the_lock() -> None:
    """Third-party versions and hashes come from `uv.lock`, not an unpinned PyPI resolve.

    The install happens in the builder stage — the final stage has no pip. The workspace's own
    wheels go in afterwards without dependency resolution, and `pip check` proves that nothing
    is missing.
    """
    instructions = _all_instructions()
    assert any(
        "pip install --require-hashes --no-deps -r /wheels/requirements.txt" in ln
        for ln in instructions
    )
    assert any("pip install --no-deps /wheels/*.whl" in ln for ln in instructions)
    assert any("pip check" in ln for ln in instructions)


def test_final_stage_installs_no_compilers() -> None:
    """Build tools stay in the builder stage; the final stage installs only runtime packages."""
    text = " ".join(_final_stage())
    for tool in ("build-essential", "gcc", "g++", "make"):
        assert tool not in text


def test_final_stage_installs_git_and_ssh_for_deploys() -> None:
    """The worker fetches repositories with the git binary, over https or ssh."""
    text = " ".join(_final_stage())
    assert "git" in text.split()
    assert "openssh-client" in text.split()
    assert "--no-install-recommends" in text


def test_final_stage_ships_uv_pinned_by_digest() -> None:
    """The `uv` binary on the worker's PATH, from a stage pinned by tag and digest.

    The pin is a `FROM` line because Dependabot's docker ecosystem does not read the image of a
    `COPY --from=<image>` (dependabot-core #5103); `COPY --from=uv` then takes the binary.
    """
    pinned = re.compile(r"^FROM ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+@sha256:[0-9a-f]{64} AS uv$")
    assert [ln for ln in _all_instructions() if pinned.match(ln)], _all_instructions()
    assert "COPY --from=uv /uv /usr/local/bin/uv" in _final_stage()


def test_final_stage_owns_the_worker_cache_directory_privately() -> None:
    """The venv cache must be owned by uid 10001 and mode 0700, or every Python task fails.

    A named volume mounted over an existing directory inherits its owner and mode on first use.
    """
    text = " ".join(_final_stage())
    assert "mkdir -p" in text
    assert re.search(r"chown -R lakana:lakana (/\S+ )*/home/lakana( /\S+)*(?= |$)", text), text
    assert "chmod 0700 /home/lakana/.cache/lakana-worker" in text
    assert "/home/lakana/.cache/lakana-worker" in text


def test_the_image_prepares_the_directories_the_launcher_and_the_worker_mount() -> None:
    """A named volume takes owner and mode from the image's directory on first use."""
    text = _DOCKERFILE.read_text(encoding="utf-8")

    assert "install -d -o 0 -g 10001 -m 0770 /run/lakana-launcher" in text
    # Sticky as well as set-group-id: the worker's group can add files but never rename or
    # delete the token or the socket, so no link can be swapped in under the launcher.
    assert "chmod 3770 /run/lakana-launcher" in text
    assert "install -d -o 0 -g 0 -m 0700 /var/lib/lakana-launcher" in text


_LAUNCHER_COMMAND = (
    "/usr/local/bin/python -I -S -c \"import sys; sys.path.insert(0, '/opt/lakana-launcher'); "
    'from lakana_worker.launcher.__main__ import main; main()"'
)


def test_the_image_carries_an_isolated_copy_of_the_launcher_and_the_command_to_run_it() -> None:
    """The launcher holds the Docker socket: no `.pth` file of the venv may run in it.

    The bundle's `launcher` service runs `_LAUNCHER_COMMAND` (the base interpreter, `-I` for
    no environment and no user site, `-S` for no site at all, and only the copy on the path).
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")

    assert "/opt/lakana-launcher/lakana_worker/launcher/" in text
    assert "touch /opt/lakana-launcher/lakana_worker/__init__.py" in text
    assert f"# launcher command: {_LAUNCHER_COMMAND}" in text
    assert " -S " in _LAUNCHER_COMMAND and " -I " in _LAUNCHER_COMMAND


_EGRESS_COMMAND = (
    "/usr/local/bin/python -I -S -c \"import sys; sys.path.insert(0, '/opt/lakana-launcher'); "
    'from lakana_worker.launcher.egress import main; main()"'
)


def _service(name: str) -> dict:
    """Return one service of the Compose bundle.

    Parameters
    ----------
    name
        The service.

    Returns
    -------
    dict
        Its definition.
    """
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"][name]


def test_the_bundle_runs_the_launcher_and_the_proxy_with_the_commands_the_image_names() -> None:
    """The image and the bundle agree: both services start on the isolated copy, `-I -S`.

    The egress proxy reads the key that signs every build's egress, so no `.pth` file of a
    dependency may run in it either; it needs only the standard library, like the launcher.
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")

    assert f"# egress command: {_EGRESS_COMMAND}" in text
    assert _service("launcher")["command"] == shlex.split(_LAUNCHER_COMMAND)
    assert _service("egress")["command"] == shlex.split(_EGRESS_COMMAND)


def test_the_egress_key_is_readable_by_the_proxy_and_by_no_other_user_of_the_image() -> None:
    """The key directory is 2750 root:10001 and the proxy runs as 10001:10001.

    The launcher (root) writes the key 0640; the set-group-id directory gives it group 10001, so
    the proxy reads it through its group and nobody else through the file's mode. The workers
    share that group, so they must not mount the volume (`test_deploy_init.py` pins who does).
    """
    text = " ".join(_final_stage())

    assert "install -d -o 0 -g 10001 -m 2750 /run/lakana-egress" in text
    assert _service("egress")["user"] == "10001:10001"
    assert "USER 10001:10001" in _final_stage()


def test_the_image_gives_the_task_work_volume_the_workers_owner_and_mode() -> None:
    """The launcher's mount of `task-work` is the first, so the fresh volume takes this directory.

    The workers' mount point lies inside the shared `worker-cache` volume, where the image may
    create nothing, so the owner (uid 10001) and mode (0700) the worker requires of its attempt
    directory come from the launcher's mount point instead.
    """
    text = " ".join(_final_stage())

    assert "install -d -o 10001 -g 10001 -m 0700 /var/lib/lakana-task-work" in text
    assert "task-work:/var/lib/lakana-task-work" in _service("launcher")["volumes"]
    assert _service("worker")["depends_on"]["launcher"] == {"condition": "service_healthy"}


def test_the_image_creates_nothing_below_the_mount_point_of_a_shared_volume() -> None:
    """Docker copies the image's content into a new named volume at its first mount.

    Two containers that mount one empty volume at the same moment both copy, and the second
    fails on `mkdir ...: file exists`. The mount point itself may be prepared (a fresh volume
    takes its owner and mode); anything inside is made by the program that needs it.
    """
    mounts = [m for volume in _SHARED_VOLUMES for m in [_mount_point(volume)]]
    created = _created_directories()
    assert created, "the Dockerfile creates no directory at all: the parser found nothing"
    for mount in mounts:
        below = [d for d in created if d != mount and d.startswith(f"{mount}/")]
        assert below == [], f"the image creates {below} inside the shared volume at {mount}"
    # the mount point stays, so a fresh volume inherits its owner and mode
    assert "/home/lakana/.cache/lakana-worker" in created


_SHARED_VOLUMES = ("worker-cache", "launcher-socket", "task-work")
"""Named volumes that more than one container mounts (checked against `compose.yaml`)."""


def _mount_point(volume: str) -> str:
    """Return where the Compose bundle mounts a named volume in the worker.

    Parameters
    ----------
    volume
        The volume's name.

    Returns
    -------
    str
        The container path.

    Raises
    ------
    AssertionError
        If the worker mounts no such volume.
    """
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    for entry in compose["services"]["worker"]["volumes"]:
        source, _, rest = entry.partition(":")
        if source == volume:
            return rest.split(":")[0].rstrip("/")
    raise AssertionError(f"the worker does not mount {volume}")


def _created_directories() -> list[str]:
    """List the absolute paths the final stage creates with `mkdir` or `install -d`.

    Returns
    -------
    list of str
        Paths, without a trailing slash.
    """
    found: list[str] = []
    for ln in _final_stage():
        for part in re.split(r"&&|;", ln):
            words = part.split()
            if words[:1] == ["RUN"]:
                words = words[1:]
            if words[:1] == ["mkdir"] or words[:2] == ["install", "-d"]:
                found += [w.rstrip("/") for w in words if w.startswith("/")]
    return found


_TASK_DOCKERFILE = _DOCKERFILE.with_name("Dockerfile.task")


def test_the_task_image_carries_python_uv_and_the_build_entry_and_no_entrypoint() -> None:
    """The launcher sets the command; an entrypoint in the image would run first."""
    text = _TASK_DOCKERFILE.read_text(encoding="utf-8")

    assert "ENTRYPOINT" not in text
    assert "FROM py${PYTHON}" in text
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert re.search(
            rf"^FROM python:{version}-slim@sha256:[0-9a-f]{{64}} AS py{version}$", text, re.M
        )
    assert (
        "COPY packages/lakana-worker/src/lakana_worker/launcher/task_build.py"
        " /opt/lakana-task/build.py" in text
    )
    assert "--uid 10001" in text and "--uid 10002" in text
    assert "--gid 10003" in text
    assert "install -d -o 10002 -g 10002 -m 0755 /env" in text
    assert "USER 10001:10003" in text
    ignore = _TASK_DOCKERFILE.with_name("Dockerfile.task.dockerignore").read_text(encoding="utf-8")
    assert ignore.splitlines() == [
        "*",
        "!packages/lakana-worker/src/lakana_worker/launcher/task_build.py",
        "!packages/lakana/src/lakana/ingest/_check.py",
    ]


def _task_run_steps() -> list[str]:
    """Join the task image's ``RUN`` instructions with their continuation lines.

    Returns
    -------
    list of str
        One string per ``RUN``, its lines joined with single spaces.
    """
    steps: list[str] = []
    current: list[str] | None = None
    for line in _TASK_DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if current is None and line.startswith("RUN "):
            current = []
        if current is not None:
            current.append(line.rstrip("\\").strip())
            if not line.endswith("\\"):
                steps.append(" ".join(current))
                current = None
    return steps


def test_the_task_image_holds_lakanas_own_check_and_runs_it_as_the_launcher_will() -> None:
    """`/opt/lakana-check/check.py`: the standard-library check, copied, never a project's code.

    The launcher's ``check`` op runs ``/usr/local/bin/python -I -B /opt/lakana-check/check.py``
    (`CHECK_ENTRY`); the build runs it the same way once, and nothing but that file is added:
    no wheel, no dependencies.
    """
    text = _TASK_DOCKERFILE.read_text(encoding="utf-8")
    copy = "COPY packages/lakana/src/lakana/ingest/_check.py /opt/lakana-check/check.py"
    (step,) = [s for s in _task_run_steps() if "/opt/lakana-check" in s]

    assert copy in text
    assert "/usr/local/bin/python -I -B /opt/lakana-check/check.py" in step
    assert "does not know the kind" in step
    # Root's and read-only, whatever owner and mode the file had in the build context.
    assert step.startswith(
        "RUN chown 0:0 /opt/lakana-check/check.py && chmod 0444 /opt/lakana-check/check.py && "
    )
    assert "dist/" not in text and "pip install" not in text
    run = next(
        i
        for i, line in enumerate(text.splitlines())
        if line.startswith("RUN ") and ("/opt/lakana-check" in line)
    )
    lines = text.splitlines()
    assert lines.index(copy) < run
    assert run < next(i for i, line in enumerate(lines) if "find / -xdev -perm /6000" in line)
    assert run < lines.index("USER 10001:10003")


def test_the_task_image_pins_the_same_uv_as_the_main_image() -> None:
    """A venv is built and run with the image's uv; both images move together."""

    def uv_line(path: Path) -> str:
        return next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("FROM ghcr.io/astral-sh/uv:")
        )

    assert uv_line(_TASK_DOCKERFILE) == uv_line(_DOCKERFILE)


def test_the_task_image_drops_every_setuid_and_setgid_bit() -> None:
    """Without no-new-privileges (snap Docker) a setuid `su` or `mount` would be uid 0 again.

    The real-engine test (`test_docker_isolation_integration.py`) finds no such file in the
    built image; this one keeps the step in place and before `USER`.
    """
    text = _TASK_DOCKERFILE.read_text(encoding="utf-8")
    step = "find / -xdev -perm /6000 -type f -exec chmod a-s {} +"

    assert step in text
    assert text.index(step) < text.index("USER 10001:10003")
    assert text.index(step) > text.index("useradd --system --uid 10002")
