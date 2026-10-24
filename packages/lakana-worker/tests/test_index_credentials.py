"""The credential of a registered index reaches uv and nothing else."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess  # nosec B404 -- the real uv with fixed argv
import sys
import uuid
from pathlib import Path

import index_fixture
import pytest
from index_fixture import PASSWORD, USERNAME, Index, project_files, serving
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_worker.index_credentials as index_credentials
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.models import Environment, Principal, PrincipalType, Secret, SecretUseKind
from lakana_core.projects.package_sources import RegisteredIndex
from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse
from lakana_worker.index_credentials import (
    INDEX_NAME_PREFIX,
    IndexAccess,
    IndexCredentialError,
    encodable,
    invalid_url_message,
    missing_wheel,
    no_wheel_message,
    redaction_values,
    refusal_message,
    refused_index,
    resolve_index_access,
    uv_index_value,
    wheels_only,
)
from lakana_worker.venvs import VenvSettings, build_environment

_REFUSED_401 = """\
  \N{MULTIPLICATION SIGN} Failed to download `team-utils==1.4.0`
  ├─▶ Failed to fetch:
  │   `https://pkgs.example.org/files/team_utils-1.4.0-py3-none-any.whl`
  ╰─▶ HTTP status client error (401 Unauthorized) for url
      (https://pkgs.example.org/files/team_utils-1.4.0-py3-none-any.whl)
"""
_NO_WHEEL = (
    "error: Distribution `sdonly==1.0.0 @ registry+https://pkgs.example.org/simple` can't be "
    "installed because it is marked as `--no-build` but has no binary distribution\n"
)


def _uv() -> str:
    """Find the uv that runs this test session.

    Returns
    -------
    str
        The path or name of the executable.
    """
    found = os.environ.get("UV") or shutil.which("uv")
    assert found, "these tests need uv; run them with `uv run pytest`"
    return found


def _access(index: Index, password: str | None = PASSWORD, **changes: str) -> IndexAccess:
    """Describe the registered index as the worker would.

    Parameters
    ----------
    index
        The fixture index.
    password
        The secret's password, or `None` for an index without a secret.
    **changes
        Fields to override.

    Returns
    -------
    IndexAccess
        The access.
    """
    fields = {
        "name": "team",
        "url": index.url(),
        "environment": "dev",
        "secret": None if password is None else "team-index",
        "username": USERNAME,
        **changes,
    }
    return IndexAccess(password=password, **fields)  # type: ignore[arg-type]


def _sync(
    root: Path,
    files: dict[str, str],
    accesses: list[IndexAccess],
    *,
    no_config: bool = True,
    binaries: dict[str, bytes] | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, Path]:
    """Run `uv sync` as `lakana_worker.venvs._build` does.

    Parameters
    ----------
    root
        Directory holding the project, the build's HOME, the venv and uv's cache.
    files
        The project's files.
    accesses
        The registered indexes of the build.
    no_config
        Pass `--no-config`, as the worker does; `False` only to show that a test can fail.
    binaries
        More files of the project, by relative path (a `wheelhouse` folder).
    extra_env
        More variables for uv, on top of the worker's.

    Returns
    -------
    tuple
        Exit status, combined output and `root`.
    """
    project, home = root / "code", root / "home"
    project.mkdir(parents=True)
    home.mkdir()
    for name, text in files.items():
        (project / name).write_text(text, encoding="utf-8")
    for name, data in (binaries or {}).items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(data)
    settings = VenvSettings(uv=_uv(), cache=root / "venvs", uv_cache=root / "uv-cache")
    env = build_environment(settings, home, root / "venv")
    env.update(extra_env or {})
    value = uv_index_value(None, accesses)
    if value is not None:
        env["UV_INDEX"] = value
    argv = [
        _uv(),
        "sync",
        "--frozen",
        *(["--no-config"] if no_config else []),
        "--no-dev",
        "--no-default-groups",
        "--no-install-project",
        "--no-install-workspace",
        *(["--no-build"] if wheels_only(accesses) else []),
        "--python",
        sys.executable,
        "--project",
        str(project),
    ]
    done = subprocess.run(  # nosec B603 -- fixed argv, no shell
        argv,
        env=env,
        cwd=home,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout.decode("utf-8", "replace"), root


def _files_holding(root: Path, secret: str) -> list[str]:
    """List the files under a directory that contain a secret.

    Parameters
    ----------
    root
        The directory.
    secret
        The text to look for.

    Returns
    -------
    list of str
        Paths relative to `root`.
    """
    needle = secret.encode()
    return [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
    ]


def test_the_build_installs_from_the_registered_index_and_leaves_the_credential_nowhere(
    tmp_path: Path,
) -> None:
    """The mechanism: an index the worker defines in `UV_INDEX`, the credential in its URL."""
    with serving() as indexes:
        code, output, root = _sync(
            tmp_path, project_files(indexes.registered.url()), [_access(indexes.registered)]
        )
        assert code == 0, output
        assert indexes.registered.credentials_received == [f"{USERNAME}:{PASSWORD}"]
    installed = next(root.glob("venv/lib/*/site-packages/team_utils/__init__.py"))
    assert installed.read_text() == "VALUE = 42\n"
    assert PASSWORD not in output
    # The project's directory, the venv, uv's cache and the build's HOME: all of `root`.
    assert _files_holding(root, PASSWORD) == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("challenge", [False, True])
def test_a_project_that_names_another_host_gives_that_host_no_credential(
    tmp_path: Path, host: str, challenge: bool
) -> None:
    """The invariant. `127.0.0.1` is the registered index's own host on another port."""
    with serving() as indexes:
        indexes.other.server.challenge_everything = challenge
        code, output, _ = _sync(
            tmp_path, project_files(indexes.other.url(host)), [_access(indexes.registered)]
        )
        assert indexes.other.server.seen, "the build never asked the other host"
        assert indexes.other.credentials_received == []
        assert indexes.registered.server.seen == []
    assert (code == 0) is (not challenge), output
    assert PASSWORD not in output


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_a_redirect_to_another_host_carries_no_credential(tmp_path: Path, host: str) -> None:
    """An index that hands its files to another host or port: it gets the request only."""
    with serving() as indexes:
        indexes.registered.server.redirect_files_to = f"http://{host}:{indexes.other.port}"
        code, output, _ = _sync(
            tmp_path, project_files(indexes.registered.url()), [_access(indexes.registered)]
        )
        assert code == 0, output
        assert indexes.other.server.seen
        assert all(header is None for _, header in indexes.other.server.seen)
    assert PASSWORD not in output


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("challenge", [False, True])
def test_a_project_index_with_the_workers_generated_name_gets_no_credential(
    tmp_path: Path, host: str, challenge: bool
) -> None:
    """The credential follows the URL, not the name: a project may reuse `lakana-registered-0`."""
    with serving() as indexes:
        indexes.other.server.challenge_everything = challenge
        files = project_files(indexes.other.url(host))
        files["pyproject.toml"] = files["pyproject.toml"].replace(
            '"team"', f'"{INDEX_NAME_PREFIX}0"'
        )
        assert INDEX_NAME_PREFIX in files["pyproject.toml"]
        code, output, _ = _sync(tmp_path, files, [_access(indexes.registered)])
        assert indexes.other.server.seen, "the build never asked the other host"
        assert all(header is None for _, header in indexes.other.server.seen)
        assert indexes.registered.server.seen == []
    assert (code == 0) is (not challenge), output
    assert PASSWORD not in output


def _hostile_settings(registered_port: int, proxy_port: int) -> dict[str, str]:
    """Write the project of review C1: a proxy of the project's choosing for the registered host.

    Parameters
    ----------
    registered_port
        The registered index's port.
    proxy_port
        The port of the recording host that plays the project's proxy.

    Returns
    -------
    dict of str to str
        The project's files; the lock names the registered index on a host that does not exist,
        so only a proxy can answer for it.
    """
    files = project_files(f"http://registered.invalid:{registered_port}/simple")
    files["pyproject.toml"] += (
        f'\n[tool.uv]\nhttp-proxy = "http://127.0.0.1:{proxy_port}"\n'
        'allow-insecure-host = ["registered.invalid"]\n'
    )
    return files


def test_a_project_s_uv_settings_cannot_route_the_credential_to_a_proxy(tmp_path: Path) -> None:
    """Review C1, with `--no-config`: the recording host receives no request at all."""
    with serving() as indexes:
        files = _hostile_settings(indexes.registered.port, indexes.other.port)
        access = _access(
            indexes.registered, url=f"http://registered.invalid:{indexes.registered.port}/simple"
        )
        # No retries: uv cannot resolve `registered.invalid` and would back off for 10 s, which
        # shows nothing more than the first failure does.
        code, output, root = _sync(tmp_path, files, [access], extra_env={"UV_HTTP_RETRIES": "0"})
        assert indexes.other.server.seen == []
        assert indexes.other.credentials_received == []
        assert indexes.registered.server.seen == []
    assert code != 0, output
    assert PASSWORD not in output
    assert _files_holding(root, PASSWORD) == []


def test_without_no_config_the_same_project_does_send_the_credential_to_its_proxy(
    tmp_path: Path,
) -> None:
    """The control of the test above: it can fail, and shows what `--no-config` prevents."""
    with serving() as indexes:
        files = _hostile_settings(indexes.registered.port, indexes.other.port)
        access = _access(
            indexes.registered, url=f"http://registered.invalid:{indexes.registered.port}/simple"
        )
        _sync(tmp_path, files, [access], no_config=False)
        assert indexes.other.credentials_received == [f"{USERNAME}:{PASSWORD}"]


def test_a_flat_wheelhouse_lock_installs_with_no_config(tmp_path: Path) -> None:
    """The `wheelhouse` folder: the lock `uv lock` writes, wheel paths relative to the folder."""
    wheel = index_fixture.WHEEL_NAME
    files = project_files("http://127.0.0.1:1/simple")
    files["pyproject.toml"] = (
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["team-utils==1.4.0"]\n\n'
        '[[tool.uv.index]]\nname = "wheelhouse"\nurl = "wheelhouse"\nformat = "flat"\n'
        "default = true\n"
    )
    files["uv.lock"] = (
        'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
        '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
        'dependencies = [\n    { name = "team-utils" },\n]\n\n'
        "[package.metadata]\n"
        'requires-dist = [{ name = "team-utils", specifier = "==1.4.0" }]\n\n'
        '[[package]]\nname = "team-utils"\nversion = "1.4.0"\n'
        'source = { registry = "wheelhouse" }\n'
        f'wheels = [\n    {{ path = "{wheel}" }},\n]\n'
    )
    binaries = {f"wheelhouse/{wheel}": index_fixture.FILES[wheel]}
    code, output, root = _sync(tmp_path, files, [], binaries=binaries)
    assert code == 0, output
    assert next(root.glob("venv/lib/*/site-packages/team_utils/__init__.py")).exists()


@pytest.mark.parametrize("password", ["right-password-RRRR", "wrong-password-WWWW"])
def test_no_form_of_the_credential_is_in_the_output_or_in_any_file(
    tmp_path: Path, password: str
) -> None:
    """Right or wrong, no value of `redaction_values` appears in uv's output or on disk."""
    with serving({"": f"{USERNAME}:right-password-RRRR"}) as indexes:
        access = _access(indexes.registered, password)
        code, output, root = _sync(tmp_path, project_files(indexes.registered.url()), [access])
    assert (code == 0) is (password == "right-password-RRRR"), output
    values = redaction_values([access])
    assert values
    for value in values:
        assert value not in output
        assert _files_holding(root, value) == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_credential_is_recognised_and_never_shown(tmp_path: Path, status: int) -> None:
    """Uv's text for a refusal names the URL; the worker's message names index and secret."""
    with serving() as indexes:
        indexes.registered.server.refusal = status
        accesses = [_access(indexes.registered, "wrong-password")]
        code, output, _ = _sync(tmp_path, project_files(indexes.registered.url()), accesses)
        assert code != 0
        refused = refused_index(output, accesses)
        elsewhere = refused_index(output, [_access(indexes.other)])
    assert refused is accesses[0]
    assert elsewhere is None
    assert "wrong-password" not in output
    assert refusal_message(accesses[0]) == (
        "The package index 'team' refused the credential in secret 'team-index' of environment "
        "'dev'. Set it again with `lakana secret rotate team-index --env dev --values-stdin`. "
        "If the password is right, check that its account may read the package: the index "
        "also answers 403 when the account may not read a package or file."
    )


def test_an_index_without_a_registered_credential_that_asks_for_one(tmp_path: Path) -> None:
    """No `UV_INDEX` entry, no `--no-build`; the refusal says a credential must be bound."""
    with serving() as indexes:
        accesses = [_access(indexes.registered, None)]
        assert uv_index_value(None, accesses) is None
        assert not wheels_only(accesses)
        code, output, _ = _sync(tmp_path, project_files(indexes.registered.url()), accesses)
        assert code != 0
        assert refused_index(output, accesses) is accesses[0]
    assert refusal_message(accesses[0]) == (
        "The package index 'team' asked for a credential, and none is registered for it. An "
        "administrator binds one with `lakana index update team --credential-secret NAME` and "
        "creates the secret NAME in environment 'dev'."
    )


def test_a_package_with_only_a_source_distribution_is_refused(tmp_path: Path) -> None:
    """Rule 4: with a credential in play uv runs with `--no-build` and builds nothing."""
    with serving() as indexes:
        accesses = [_access(indexes.registered)]
        code, output, _ = _sync(
            tmp_path, project_files(indexes.registered.url(), "sdonly"), accesses
        )
        assert indexes.registered.server.seen == []
    assert code != 0
    assert missing_wheel(output) == "sdonly"
    assert no_wheel_message("sdonly") == (
        "sdonly has no wheel for this platform and Python; publish a wheel to your index, or "
        "remove the dependency. A project that uses a package index with a credential installs "
        "wheels only."
    )


@pytest.mark.parametrize(
    "password",
    [
        "with space",
        "hash#and?query",
        "quote\"single'",
        "colon:at@slash/",
        "ümlaut-pässwörd-日本",
        "percent%41",
        "back\\slash",
        "line\nbreak",
    ],
)
def test_a_credential_with_awkward_characters_reaches_the_index_intact(
    tmp_path: Path, password: str
) -> None:
    """Every character is percent-encoded on the way and decoded by uv."""
    username = "us er@corp"
    with serving({"": f"{username}:{password}"}) as indexes:
        access = _access(indexes.registered, password, username=username)
        code, output, root = _sync(tmp_path, project_files(indexes.registered.url()), [access])
        assert code == 0, output
        assert indexes.registered.credentials_received == [f"{username}:{password}"]
    assert password in redaction_values([access])
    assert f"{username}:{password}" in redaction_values([access])
    assert _files_holding(root, password) == []


def test_an_empty_user_name_is_sent_as_a_colon_and_the_password(tmp_path: Path) -> None:
    """Some indexes take a token as the password with no user name."""
    with serving({"": ":token-only-Pw"}) as indexes:
        access = _access(indexes.registered, "token-only-Pw", username="")
        code, output, root = _sync(tmp_path, project_files(indexes.registered.url()), [access])
        assert code == 0, output
        assert indexes.registered.credentials_received == [":token-only-Pw"]
    for value in redaction_values([access]):
        assert value not in output
        assert _files_holding(root, value) == []


@pytest.mark.parametrize("first", ["/t1", "/t2"])
def test_two_indexes_on_one_host_each_get_their_own_credential(tmp_path: Path, first: str) -> None:
    """Two path prefixes of one host, two passwords, in either order."""
    wanted = {"/t1/": "one:pw-one-AAAA", "/t2/": "two:pw-two-BBBB"}
    with serving(wanted) as indexes:
        accesses = {
            prefix: IndexAccess(
                name, indexes.registered.url(prefix=prefix), "dev", f"{name}-index", name, secret
            )
            for prefix, (name, secret) in {
                "/t1": ("one", "pw-one-AAAA"),
                "/t2": ("two", "pw-two-BBBB"),
            }.items()
        }
        second = "/t2" if first == "/t1" else "/t1"
        code, output, _ = _sync(
            tmp_path,
            project_files(indexes.registered.url(prefix="/t2")),
            [accesses[first], accesses[second]],
        )
        assert code == 0, output
        assert indexes.registered.credentials_received == ["two:pw-two-BBBB"]


def test_the_uv_index_value_and_what_is_redacted() -> None:
    """The operator's index first; one worker-named entry per credential, percent-encoded."""
    with_credential = IndexAccess(
        "team", "https://pkgs.example.org/simple", "dev", "s", "u s", "p@:/"
    )
    without = IndexAccess("open", "https://open.example.org/simple", "dev")
    assert uv_index_value(None, []) is None
    assert uv_index_value(None, [without]) is None
    assert uv_index_value("https://op:pw@mirror.corp/simple", [without, with_credential]) == (
        "https://op:pw@mirror.corp/simple "
        f"{INDEX_NAME_PREFIX}0=https://u%20s:p%40%3A%2F@pkgs.example.org/simple"
    )
    assert wheels_only([without, with_credential])
    assert not wheels_only([without])
    values = redaction_values([without, with_credential])
    assert {"p@:/", "p%40%3A%2F", "u s:p@:/", "u%20s:p%40%3A%2F", "dSBzOnBAOi8="} <= set(values)
    assert redaction_values([without]) == []
    assert "p@:/" not in repr(with_credential)
    assert "u s" not in repr(with_credential)


def test_uv_s_texts_are_recognised_as_they_were_probed() -> None:
    """The exact texts of uv 0.12, so a changed wording fails without a network."""
    team = IndexAccess("team", "https://pkgs.example.org/simple", "dev", "team-index", "u", "p")
    other_port = IndexAccess("alt", "https://pkgs.example.org:8443/simple", "dev", "s", "u", "p")
    assert refused_index(_REFUSED_401, [other_port, team]) is team
    assert refused_index(_REFUSED_401.replace("401 Unauthorized", "403 Forbidden"), [team]) is team
    assert refused_index(_REFUSED_401.replace("401 Unauthorized", "404 Not Found"), [team]) is None
    assert refused_index(_REFUSED_401.replace("https://pkgs", "ftp://pkgs"), [team]) is None
    assert refused_index(_REFUSED_401.replace("example.org/", "example.org:x/"), [team]) is None
    assert refused_index("Resolved 2 packages", [team]) is None
    assert missing_wheel(_NO_WHEEL) == "sdonly"
    assert missing_wheel(_NO_WHEEL.replace("sdonly==", "bad name\x1b[31m==")) is None
    assert missing_wheel("Installed 1 package") is None


def test_a_refusal_names_the_index_whose_path_the_refused_url_is_under() -> None:
    """Two indexes on one host and port: the longest matching path prefix wins."""
    one = IndexAccess("one", "https://pkgs.example.org/t1/simple", "dev", "one-index", "u", "p")
    two = IndexAccess("two", "https://pkgs.example.org/t2/simple", "dev", "two-index", "u", "p")
    open_ = IndexAccess("open", "https://pkgs.example.org/open/simple", "dev")
    root = IndexAccess("root", "https://pkgs.example.org/simple", "dev", "r", "u", "p")

    def refused(path: str) -> str:
        return _REFUSED_401.replace("/files/team_utils", path + "/files/team_utils")

    assert refused_index(refused("/t2"), [one, two]) is two
    assert refused_index(refused("/t2"), [open_, two]) is two
    assert refused_index(refused("/t1"), [two, one]) is one
    assert refused_index(refused("/t22"), [one, two]) is one
    assert refused_index(refused("/other"), [one, two]) is one
    assert refused_index(refused("/t2"), [root, two]) is two
    assert refused_index(refused("/t3"), [two, root]) is root


def test_a_refusal_names_the_right_one_of_two_devpi_indexes_on_one_host() -> None:
    """Devpi serves an index at `…/+simple` and its files at `…/+f/…` beside it."""
    a = IndexAccess("a", "https://devpi.example.org/root/a/+simple", "dev", "a-index", "u", "p")
    b = IndexAccess("b", "https://devpi.example.org/root/b/+simple", "dev", "b-index", "u", "p")
    refused = _REFUSED_401.replace(
        "https://pkgs.example.org/files/", "https://devpi.example.org/root/b/+f/0a1/b2c3/"
    )
    assert "devpi.example.org/root/b/+f/0a1/b2c3/team_utils-1.4.0" in refused
    assert refused_index(refused, [a, b]) is b
    assert refused_index(refused, [b, a]) is b
    assert refused_index(refused.replace("/root/b/", "/root/a/"), [a, b]) is a
    assert refused_index(refused.replace("/root/b/", "/root/a/"), [b, a]) is a


def test_a_value_that_cannot_be_encoded_is_refused_without_naming_it() -> None:
    """A lone surrogate is not UTF-8: a `ValueError` with a fixed message, never the value."""
    assert encodable("ümlaut-日本")
    assert encodable("")
    assert not encodable("a\ud800b")
    for username, password in [("u", "a\ud800b"), ("a\ud800b", "p")]:
        access = IndexAccess(
            "team", "https://pkgs.example.org/simple", "dev", "s", username, password
        )
        for call in (
            lambda a=access: uv_index_value(None, [a]),
            lambda a=access: redaction_values([a]),
        ):
            with pytest.raises(ValueError, match="cannot be sent") as raised:
                call()
            assert "ud800" not in str(raised.value)
            assert not isinstance(raised.value, UnicodeEncodeError)


def test_an_empty_password_is_not_redacted() -> None:
    """`:` and its base64 form would blank ordinary text; a normal password keeps its forms."""
    empty = IndexAccess("team", "https://pkgs.example.org/simple", "dev", "s", "", "")
    assert redaction_values([empty]) == []
    assert (
        redaction_values([IndexAccess("t", "https://pkgs.example.org/simple", "dev", "s", "u", "")])
        == []
    )
    assert (
        uv_index_value(None, [empty]) == f"{INDEX_NAME_PREFIX}0=https://:@pkgs.example.org/simple"
    )
    normal = IndexAccess("team", "https://pkgs.example.org/simple", "dev", "s", "", "pw")
    assert {"pw", ":pw"} <= set(redaction_values([normal]))


def test_serving_closes_the_first_socket_when_the_second_bind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A setup failure leaks no listening socket."""
    real = index_fixture._Server
    made: list[index_fixture._Server] = []

    def second_fails(credentials: dict[str, str]) -> index_fixture._Server:
        if made:
            raise OSError("address already in use")
        made.append(real(credentials))
        return made[0]

    monkeypatch.setattr(index_fixture, "_Server", second_fails)
    with pytest.raises(OSError, match="address already in use"), serving():
        pass  # pragma: no cover -- never reached
    assert made[0].socket.fileno() == -1


_KEY = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
_TEAM = RegisteredIndex(
    "team",
    "https://pkgs.example.org/simple",
    "team-index",
    id=uuid.UUID("00000000-0000-0000-0000-00000000a001"),
)
_OPEN = RegisteredIndex("open", "https://open.example.org/simple")


def _environment(session: Session, name: str) -> Environment:
    """Persist an environment.

    Parameters
    ----------
    session
        Database session.
    name
        The environment's name.

    Returns
    -------
    Environment
        The stored row.
    """
    row = Environment(
        id=uuid.uuid4(), name=name, storage_root=f"file:///tmp/{name}", storage_options={}
    )
    session.add(row)
    session.commit()
    return row


def _secret(session: Session, environment: str, values: dict[str, str]) -> uuid.UUID:
    """Create the secret `team-index` in an environment.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    values
        The secret's values.

    Returns
    -------
    uuid.UUID
        The secret's id.
    """
    owner = PrincipalRef(id=uuid.uuid4(), name=f"owner-{environment}", type=PrincipalType.USER)
    session.add(Principal(id=owner.id, type=owner.type, name=owner.name, is_active=True))
    session.commit()
    secret = SecretService(session, AllowAllAuthorization(), _KEY).create(
        owner, environment=environment, name="team-index", values=values
    )
    # The index has no row in these tests, so the binding is written as the row holds it.
    secret.use_kind, secret.use_target_id = SecretUseKind.PACKAGE_INDEX, _TEAM.id
    session.commit()
    return secret.id


def test_resolve_reads_the_credential_from_the_environments_own_secret(session: Session) -> None:
    dev, prod = _environment(session, "dev"), _environment(session, "prod")
    _secret(session, "dev", {"username": "ci", "password": "dev-password"})
    _secret(session, "prod", {"password": "prod-password"})
    (team, plain) = resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM, _OPEN])
    assert (team.name, team.url, team.environment, team.secret, team.username, team.password) == (
        "team",
        "https://pkgs.example.org/simple",
        "dev",
        "team-index",
        "ci",
        "dev-password",
    )
    assert (plain.secret, plain.username, plain.password) == (None, "", None)
    (in_prod,) = resolve_index_access(session, _KEY, prod.id, prod.name, [_TEAM])
    assert (in_prod.username, in_prod.password) == ("", "prod-password")
    assert resolve_index_access(session, None, dev.id, dev.name, [_OPEN]) == [plain]


def test_a_missing_or_revoked_secret_names_the_command_that_creates_it(session: Session) -> None:
    dev, prod = _environment(session, "dev"), _environment(session, "prod")
    secret_id = _secret(session, "prod", {"username": "ci", "password": "p"})
    expected = (
        "the package index 'team' needs the secret 'team-index' in environment 'dev', which "
        "does not exist. Create it with `lakana secret create team-index --env dev "
        '--index team --values-stdin` (a JSON object with "username" and "password")'
    )
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])
    assert str(caught.value) == expected
    owner = PrincipalRef(id=uuid.uuid4(), name="revoker", type=PrincipalType.USER)
    session.add(Principal(id=owner.id, type=owner.type, name=owner.name, is_active=True))
    session.commit()
    SecretService(session, AllowAllAuthorization(), _KEY).revoke(owner, secret_id)
    with pytest.raises(IndexCredentialError, match="in environment 'prod', which does not exist"):
        resolve_index_access(session, _KEY, prod.id, prod.name, [_TEAM])


@pytest.mark.parametrize(
    "values",
    [
        {"username": "ci-user-91", "token": "not-the-password-key"},
        {"username": "ci-user-91", "password": "nul\x00inside"},
        {"username": "nul\x00user", "password": "fine"},
    ],
)
def test_a_secret_that_cannot_be_passed_on_is_refused(
    session: Session, values: dict[str, str]
) -> None:
    """No password, a NUL (which no environment variable can hold) or a lone surrogate."""
    dev = _environment(session, "dev")
    _secret(session, "dev", values)
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])
    assert str(caught.value) == (
        "the secret 'team-index' of environment 'dev', which the package index 'team' uses, has "
        'no usable "password". Set it again with `lakana secret rotate team-index --env dev '
        '--values-stdin` (a JSON object with "username" and "password", neither with a NUL '
        "character)"
    )
    for value in values.values():
        assert value not in str(caught.value)


def test_no_master_key_and_a_wrong_master_key(session: Session) -> None:
    dev = _environment(session, "dev")
    _secret(session, "dev", {"username": "ci", "password": "the-password"})
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, None, dev.id, dev.name, [_TEAM])
    assert str(caught.value) == (
        "the package index 'team' has a credential but no secrets master key is configured. "
        "Set `LAKANA_SECRETS__MASTER_KEY` for the server and the worker"
    )
    other = EnvKeyProvider(base64.b64encode(b"1" * 32).decode())
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, other, dev.id, dev.name, [_TEAM])
    assert "team-index" in str(caught.value)
    assert "the-password" not in str(caught.value)


def test_names_that_a_shell_would_split_are_quoted_in_the_command(session: Session) -> None:
    dev = _environment(session, "my env")
    index = RegisteredIndex("team", "https://pkgs.example.org/simple", "team;index")
    with pytest.raises(
        IndexCredentialError, match="lakana secret create 'team;index' --env 'my env'"
    ):
        resolve_index_access(session, _KEY, dev.id, dev.name, [index])


@pytest.mark.parametrize(
    "values",
    [
        {"username": "ci-user-91", "password": "lone\ud800surrogate"},
        {"username": "lone\udcffuser", "password": "fine"},
    ],
)
def test_a_lone_surrogate_is_refused_when_the_secret_is_stored(
    session: Session, values: dict[str, str]
) -> None:
    """The secret service refuses such a value first, naming only its key."""
    _environment(session, "dev")
    with pytest.raises(ValidationError) as caught:
        _secret(session, "dev", values)
    (key,) = [key for key, value in values.items() if value not in ("ci-user-91", "fine")]
    assert str(caught.value) == (
        f"the value of `{key}` is not valid text (it cannot be written as UTF-8); give it again "
        "as plain text"
    )
    for value in values.values():
        assert value not in str(caught.value)


@pytest.mark.parametrize(
    "values",
    [
        {"username": "ci-user-91", "password": "lone\ud800surrogate"},
        {"username": "lone\udcffuser", "password": "fine"},
    ],
)
def test_a_lone_surrogate_stored_by_an_earlier_release_gets_its_own_message(
    session: Session, values: dict[str, str]
) -> None:
    """A secret stored before the service refused such values still meets the index's text."""
    dev = _environment(session, "dev")
    secret_id = _secret(session, "dev", {"username": "ci", "password": "placeholder"})
    row = session.get(Secret, secret_id)
    assert row is not None
    encrypted = encrypt_secret_values(values, _KEY, SecretBinding(row.id, dev.id, row.name))
    row.ciphertext, row.nonce = encrypted.ciphertext, encrypted.nonce
    row.wrapped_data_key, row.key_id = encrypted.wrapped_data_key, encrypted.key_id
    row.format_version = encrypted.format_version
    session.commit()
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])
    assert str(caught.value) == (
        "the secret 'team-index' of environment 'dev', which the package index 'team' uses, holds "
        "a user name or password with a character that cannot be sent (a lone surrogate). Set it "
        "again with `lakana secret rotate team-index --env dev --values-stdin` (a JSON object "
        'with "username" and "password")'
    )
    assert "NUL" not in str(caught.value)
    for value in values.values():
        assert value not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://files.pythonhosted.org/simple",
        "https://pypi.org/simple",
        "https://pkgs\\.example.org/simple",
        "https://user:pw@pkgs.example.org/simple",
        "https://Pkgs.Example.org/simple/",
    ],
)
def test_a_stored_row_that_fails_the_registration_rule_stops_with_its_own_message(
    session: Session, url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A row written straight into the database is checked again before a secret is read."""
    dev = _environment(session, "dev")
    _secret(session, "dev", {"username": "ci", "password": "the-password"})
    row = RegisteredIndex("te\nam\x1b[2J", url, "team-index")
    with (
        caplog.at_level("WARNING", logger="lakana_worker.index_credentials"),
        pytest.raises(IndexCredentialError) as caught,
    ):
        resolve_index_access(session, None, dev.id, dev.name, [row])
    text = str(caught.value)
    assert text == (
        "the stored URL of the package index 'te\\nam\\x1b[2J' is not valid any more, so no "
        "credential is sent to it. An administrator registers it again with "
        "`lakana index update 'te\\nam\\x1b[2J' --url URL`"
    )
    assert "\x1b" not in text and "\n" not in text
    assert "the-password" not in text and "pkgs" not in text and "pypi" not in text
    assert caplog.records == []


def test_the_message_about_a_stored_url_quotes_a_plain_name_as_a_shell_would() -> None:
    assert invalid_url_message(RegisteredIndex("team", "x", "s")).endswith(
        "with `lakana index update team --url URL`"
    )
    assert invalid_url_message(RegisteredIndex("my team", "x", "s")).endswith(
        "with `lakana index update 'my team' --url URL`"
    )


def test_a_stored_row_without_a_credential_is_not_checked_because_nothing_is_sent(
    session: Session,
) -> None:
    dev = _environment(session, "dev")
    row = RegisteredIndex("open", "https://test.pypi.org/simple")
    (access,) = resolve_index_access(session, None, dev.id, dev.name, [row])
    assert (access.password, access.secret) == (None, None)
    assert uv_index_value(None, [access]) is None and redaction_values([access]) == []


def test_a_valid_stored_row_logs_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    dev = _environment(session, "dev")
    _secret(session, "dev", {"password": "p"})
    with caplog.at_level("WARNING", logger="lakana_worker.index_credentials"):
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])
    assert caplog.records == []


@pytest.mark.parametrize("url", ["https://test.pypi.org/simple", "https://test.pypi.org:8443/x"])
def test_a_credential_bound_to_a_host_that_takes_none_names_the_remedy(
    session: Session, url: str
) -> None:
    """A TestPyPI row with a secret (a direct database write) is cleared, not given a URL."""
    dev = _environment(session, "dev")
    _secret(session, "dev", {"username": "ci", "password": "the-password"})
    row = RegisteredIndex("te\nam", url, "team-index")
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, None, dev.id, dev.name, [row])
    assert str(caught.value) == (
        "the package index 'te\\nam' has a credential, but its host takes none, so no "
        "credential is sent to it. An administrator removes it with "
        "`lakana index update 'te\\nam' --clear credential_secret`"
    )
    assert "--url" not in str(caught.value) and "the-password" not in str(caught.value)


@pytest.mark.parametrize(
    ("username", "kept"),
    [
        ("ci-user-91", True),
        ("deploy@corp.example", True),
        ("abcdefgh", True),
        ("builder", False),
        ("ci", False),
        ("", False),
        ("VssSessionToken", False),
        ("x-oauth-basic", False),
        ("Gitlab-Deploy-Token", False),
        ("x-bitbucket-api-token-auth", False),
        ("aws", False),
        ("__token__", False),
        ("__TOKEN__", False),
        ("gitlab-ci-token", False),
        ("x-access-token", False),
        ("oauth2accesstoken", False),
        ("token", False),
        ("user", False),
        ("oauth2", False),
    ],
)
def test_a_long_user_name_is_redacted_and_a_fixed_or_short_one_is_not(
    username: str, kept: bool
) -> None:
    """A service that puts its token into the user name must not see it echoed."""
    access = IndexAccess(
        "team", "https://pkgs.example.org/simple", "dev", "s", username=username, password="pw-1"
    )
    values = redaction_values([access])
    assert (username in values) is kept
    assert "pw-1" in values


def test_the_user_name_is_redacted_as_written_and_percent_encoded() -> None:
    access = IndexAccess(
        "team", "https://pkgs.example.org/simple", "dev", "s", username="ci user@corp", password="p"
    )
    values = redaction_values([access])
    assert "ci user@corp" in values and "ci%20user%40corp" in values


def test_the_redactor_removes_a_user_name_that_uv_prints_alone() -> None:
    from lakana_worker.git import redact

    access = IndexAccess(
        "team",
        "https://pkgs.example.org/simple",
        "dev",
        "s",
        username="tok-AbC123xyz",
        password="p",
    )
    out = redact("error: 401 for user tok-AbC123xyz at the index", redaction_values([access]))
    assert "tok-AbC123xyz" not in out and "***" in out


@pytest.mark.parametrize(
    ("username", "password", "which"),
    [
        ("u" * 4097, "pw", '"username"'),
        ("ci", "p" * 4097, '"password"'),
        ("u" * 4097, "p" * 4097, '"username" and a "password"'),
    ],
    ids=["user-name", "password", "both"],
)
def test_a_credential_over_the_bound_gets_its_own_message(
    session: Session, username: str, password: str, which: str
) -> None:
    """Refused here, at a deploy and before a run, not by a launch with a long argument."""
    dev = _environment(session, "dev")
    _secret(session, "dev", {"username": username, "password": password})
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])
    assert str(caught.value) == (
        "the secret 'team-index' of environment 'dev', which the package index 'team' uses, has "
        f"a {which} longer than 4096 characters. Set it again with "
        "`lakana secret rotate team-index --env dev --values-stdin` (a JSON object with "
        '"username" and "password")'
    )
    assert password not in str(caught.value)


def test_a_credential_at_the_bound_is_accepted(session: Session) -> None:
    dev = _environment(session, "dev")
    _secret(session, "dev", {"username": "u" * 4096, "password": "p" * 4096})
    (access,) = resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])
    assert len(access.password or "") == 4096 and len(access.username) == 4096


def test_credentials_too_long_together_are_refused_with_their_own_message(
    session: Session,
) -> None:
    """One environment string holds every index's credential; it is limited as a whole."""
    dev = _environment(session, "dev")
    owner = PrincipalRef(id=uuid.uuid4(), name="owner", type=PrincipalType.USER)
    session.add(Principal(id=owner.id, type=owner.type, name=owner.name, is_active=True))
    session.commit()
    indexes = []
    for n in range(6):
        index = RegisteredIndex(
            f"team{n}",
            f"https://pkgs{n}.example.org/simple",
            f"team-index-{n}",
            id=uuid.UUID(int=n + 1),
        )
        secret = SecretService(session, AllowAllAuthorization(), _KEY).create(
            owner,
            environment="dev",
            name=f"team-index-{n}",
            values={"username": "!" * 4096, "password": "!" * 4096},
        )
        secret.use_kind, secret.use_target_id = SecretUseKind.PACKAGE_INDEX, index.id
        indexes.append(index)
    session.commit()
    resolve_index_access(session, _KEY, dev.id, dev.name, indexes[:3])
    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, indexes)
    assert str(caught.value).startswith(
        "the credentials of the package indexes this project uses, in environment 'dev', "
        "are too long together"
    )


def test_a_secret_bound_to_another_index_is_refused_before_it_is_decrypted(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index names the secret, but the secret serves another index: nothing is read."""
    dev = _environment(session, "dev")
    _secret(session, "dev", {"password": "pw"})
    other = RegisteredIndex(
        "other",
        "https://other.example.org/simple",
        "team-index",
        id=uuid.UUID("00000000-0000-0000-0000-00000000a002"),
    )
    decrypted: list[object] = []
    monkeypatch.setattr(
        index_credentials, "decrypt_secret_row", lambda *a, **k: decrypted.append(a)
    )

    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [other])

    assert decrypted == []
    assert "bound to a package index that was removed, not to package index 'other'" in str(
        caught.value
    )
    assert "pw" not in str(caught.value)
    assert str(caught.value).endswith("lakana secret bind team-index --env dev --index other")


@pytest.mark.parametrize(
    ("use", "current"),
    [(None, "is not bound to a use yet"), (SecretUse.environment(), "is bound to its environment")],
)
def test_a_secret_outside_every_index_is_refused_with_the_command_that_binds_it(
    session: Session, use: SecretUse | None, current: str
) -> None:
    """An unbound secret, and one bound to its environment, reach no index."""
    dev = _environment(session, "dev")
    owner = PrincipalRef(id=uuid.uuid4(), name="owner", type=PrincipalType.USER)
    session.add(Principal(id=owner.id, type=owner.type, name=owner.name, is_active=True))
    session.commit()
    SecretService(session, AllowAllAuthorization(), _KEY).create(
        owner, environment="dev", name="team-index", values={"password": "pw"}, use=use
    )

    with pytest.raises(IndexCredentialError) as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])

    assert current in str(caught.value)
    assert str(caught.value).endswith("lakana secret bind team-index --env dev --index team")
    assert "pw" not in str(caught.value)


def test_an_index_built_without_an_id_is_never_served_a_secret(session: Session) -> None:
    """No id, no binding can name it: refused for good rather than served."""
    dev = _environment(session, "dev")
    _secret(session, "dev", {"password": "pw"})
    nameless = RegisteredIndex("team", _TEAM.url, "team-index")

    with pytest.raises(IndexCredentialError, match="has no id") as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [nameless])
    assert "pw" not in str(caught.value)


def test_a_secret_bound_to_a_repository_reaches_no_index(session: Session) -> None:
    """A repository's credential is not an index's, however the index names it."""
    dev = _environment(session, "dev")
    _secret(session, "dev", {"password": "pw"})
    secret = session.execute(select(Secret)).scalar_one()
    secret.use_kind, secret.use_target_id = SecretUseKind.REPOSITORY, uuid.uuid4()
    session.commit()

    with pytest.raises(IndexCredentialError, match="not to package index 'team'") as caught:
        resolve_index_access(session, _KEY, dev.id, dev.name, [_TEAM])

    assert "bound to a repository that was removed" in str(caught.value)
