"""The credential of a registered package index, on its way into `uv sync`.

**The invariant, which the tests pin:** a credential is offered only to the host (and port)
of the registered index's URL, whatever the project's files say; it is never in the project's
directory, never in the environment or the files of the task's own process, and never in a
log line or an error.

How: the worker defines each index itself in the build's `UV_INDEX` variable, with the
credential percent-encoded inside the URL (``name=https://user:password@host/path``). uv keeps
such a credential by scheme, host and port and sends it to nothing else, whatever
`pyproject.toml` and `uv.lock` say. The two alternatives fail the invariant against the uv this
was probed with (0.12): a `.netrc` matches the host without the port, and uv's per-index
variables (`UV_INDEX_<NAME>_PASSWORD`) follow the index *name*, which a project can point at
another host.

`resolve_index_access` is the third place that decrypts a secret, after `StorageResolver`
and `lakana_worker.deploy.resolve_credential`. A secret is sent only to the index it is bound
to (`lakana_core.secrets.uses`).
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import LakanaError, ValidationError
from lakana_core.models import Secret, SecretUseKind
from lakana_core.projects.package_sources import RegisteredIndex, normalize_index_url
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUse, SecretUseRefused, plain, require_use
from lakana_worker.git import proxy_secrets

INDEX_NAME_PREFIX = "lakana-registered-"
"""Prefix of the index names the worker defines in `UV_INDEX`. Nothing depends on a project
not using such a name: the credential is bound to the URL, not to the name."""

MAX_UV_INDEX_BYTES = 100_000
"""The most bytes of the assembled `UV_INDEX` value: one environment string is limited to 128 KiB
by the system, and the launch fails past that."""

MAX_CREDENTIAL_CHARS = 4096
"""The most characters of a user name and of a password each: both go into one argument of
the launch, which the system limits, and nobody's credential is longer."""

_MIN_REDACTED_USER = 8
"""The shortest user name that is redacted from uv's output: a shorter one would blank
ordinary words (`ci`, `build`) wherever they appear."""

_FIXED_USER_NAMES = frozenset(
    {
        "__token__",
        "gitlab-ci-token",
        "x-access-token",
        "x-token-auth",
        "oauth2accesstoken",
        "_json_key",
        "_json_key_base64",
        "private-token",
        "deploy-token",
        "vsssessiontoken",
        "x-oauth-basic",
        "gitlab-deploy-token",
        "x-bitbucket-api-token-auth",
        "aws",
        "token",
        "oauth2",
        "user",
    }
)
"""User names many services prescribe for a token. They are not secret, and redacting them
would blank ordinary text (and name the service in every log line). Compared in lower case."""

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SIMPLE_SEGMENT = re.compile(r"/\+?simple\Z")
_REFUSED = re.compile(
    r"HTTP status client error \((?:401 Unauthorized|403 Forbidden)\) for url[\s│]*\((\S+?)\)"
)
"""uv's report of a refused request; the URL follows on the next line of its error tree."""
_NO_WHEEL = re.compile(
    r"Distribution `([A-Za-z0-9][A-Za-z0-9._-]{0,99})==[^`\n]*` can't be installed because it is "
    r"marked as `--no-build` but has no binary distribution"
)
"""uv's report, under `--no-build`, of a package with only a source distribution or with
wheels for other platforms only."""


class IndexCredentialError(Exception):
    """A registered index's credential cannot be used; the text is safe to store and to show."""


@dataclass(frozen=True)
class IndexAccess:
    """A registered index a build uses, with its credential if it has one.

    Parameters
    ----------
    name
        The registered index's name.
    url
        Its stored URL (no userinfo).
    environment
        The environment whose secret scope was searched, for messages.
    secret
        The name of the secret, or `None` for an index registered without one.
    username
        The secret's ``username``; empty when it has none. Left out of `repr`.
    password
        The secret's ``password``; `None` for an index without a secret. Left
        out of `repr`.
    """

    name: str
    url: str
    environment: str
    secret: str | None = None
    username: str = field(default="", repr=False)
    password: str | None = field(default=None, repr=False)


def encodable(value: str) -> bool:
    """Say whether a user name or a password can be passed to uv.

    Parameters
    ----------
    value
        A user name or a password.

    Returns
    -------
    bool
        `True` when the value encodes as UTF-8; a lone surrogate does not.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _credentialed_url(access: IndexAccess) -> str:
    """Put an index's credential into its URL, percent-encoded.

    Parameters
    ----------
    access
        An index with a password.

    Returns
    -------
    str
        ``scheme://user:password@host[:port]/path``; every character of the
        user name and the password outside ``A-Za-z0-9_.-~`` is encoded.

    Raises
    ------
    ValueError
        If the user name or the password is not `encodable`.
    """
    if not (encodable(access.username) and encodable(access.password or "")):
        raise ValueError("a package index credential contains a character that cannot be sent")
    parts = urlsplit(access.url)
    userinfo = f"{quote(access.username, safe='')}:{quote(access.password or '', safe='')}"
    return urlunsplit((parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, "", ""))


def wheels_only(accesses: Sequence[IndexAccess]) -> bool:
    """Say whether a build must not run any package's own build code.

    Parameters
    ----------
    accesses
        The registered indexes the build uses.

    Returns
    -------
    bool
        `True` when at least one of them has a credential.
    """
    return any(access.password is not None for access in accesses)


def uv_index_value(operator_index: str | None, accesses: Sequence[IndexAccess]) -> str | None:
    """Build the build's `UV_INDEX`: the operator's index, then each index with a credential.

    Parameters
    ----------
    operator_index
        `LAKANA_RUN__INDEX_URL`, or `None`.
    accesses
        The registered indexes the build uses.

    Returns
    -------
    str or None
        Space-separated entries; `None` when there is none. An index without
        a credential needs no entry: with `--frozen`, uv fetches the URLs the
        lock names.

    Raises
    ------
    ValueError
        If a user name or a password is not `encodable`.
    """
    entries = [operator_index] if operator_index else []
    credentialed = [access for access in accesses if access.password is not None]
    entries += [
        f"{INDEX_NAME_PREFIX}{number}={_credentialed_url(access)}"
        for number, access in enumerate(credentialed)
    ]
    return " ".join(entries) or None


def redaction_values(accesses: Sequence[IndexAccess]) -> list[str]:
    """List every form of the credentials that must never reach a log.

    Parameters
    ----------
    accesses
        The registered indexes the build uses.

    Returns
    -------
    list of str
        For each credential what `proxy_secrets` lists for its URL: the
        password and ``user:password``, each as given and percent-encoded,
        and their basic-auth encoding; and the user name alone, as given and
        percent-encoded, when it has at least 8 characters and is not a fixed
        user name many services prescribe (`__token__`, `gitlab-ci-token`, …):
        a service may put its token there, and uv prints a user name alone.
        An empty password has none: they would blank ordinary text.

    Raises
    ------
    ValueError
        If a user name or a password is not `encodable`.
    """
    values: list[str] = []
    for access in accesses:
        if access.password:
            values += proxy_secrets(_credentialed_url(access))
        user = access.username
        if (
            access.password is not None
            and len(user) >= _MIN_REDACTED_USER
            and user.casefold() not in _FIXED_USER_NAMES
        ):
            values += [user, quote(user, safe="")]
    return list(dict.fromkeys(values))


def _origin(url: str) -> tuple[str, str, int] | None:
    """Reduce a URL to what uv binds a credential to.

    Parameters
    ----------
    url
        A URL.

    Returns
    -------
    tuple or None
        Scheme, host in lower case and port (the scheme's default when the
        URL names none); `None` for anything that is not an http(s) URL.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return parts.scheme, parts.hostname, port or _DEFAULT_PORTS[parts.scheme]


def _index_path(access: IndexAccess) -> str:
    """Give an index's URL path without its last segment ``simple`` or ``+simple``.

    Parameters
    ----------
    access
        A registered index.

    Returns
    -------
    str
        The path, without a trailing slash: the folder an index's files are
        served under. devpi serves an index at ``…/+simple`` and its files
        beside it, at ``…/+f/…``.
    """
    return _SIMPLE_SEGMENT.sub("", urlsplit(access.url).path.rstrip("/")).rstrip("/")


def _under(path: str, prefix: str) -> bool:
    """Say whether a URL path lies under a path prefix, by whole segments.

    Parameters
    ----------
    path
        A URL path.
    prefix
        A path without a trailing slash; empty matches every path.

    Returns
    -------
    bool
        `True` when `path` equals `prefix` or continues it after a slash.
    """
    return path == prefix or path.startswith(prefix + "/")


def refused_index(output: str, accesses: Sequence[IndexAccess]) -> IndexAccess | None:
    """Find the registered index that answered 401 or 403, from uv's output.

    Parameters
    ----------
    output
        What uv printed.
    accesses
        The registered indexes the build uses.

    Returns
    -------
    IndexAccess or None
        Among the indexes with the scheme, host and port of the first refused
        URL, the one whose path (without ``/simple`` or ``/+simple``) is the longest prefix of
        the URL's path, else the first of them; `None` when uv reported no
        refusal, or one from another host.
    """
    for match in _REFUSED.finditer(output):
        origin = _origin(match.group(1))
        same = [a for a in accesses if origin is not None and _origin(a.url) == origin]
        if same:
            path = urlsplit(match.group(1)).path
            matching = [a for a in same if _under(path, _index_path(a))]
            return max(matching, key=lambda a: len(_index_path(a)), default=same[0])
    return None


def missing_wheel(output: str) -> str | None:
    """Find the package uv could not install without building it.

    Parameters
    ----------
    output
        What uv printed.

    Returns
    -------
    str or None
        The package's name, or `None` when uv reported no such package.
    """
    match = _NO_WHEEL.search(output)
    return None if match is None else match.group(1)


def refusal_message(access: IndexAccess) -> str:
    """Say that an index refused a build, and what to do.

    Parameters
    ----------
    access
        The index that answered 401 or 403.

    Returns
    -------
    str
        The message; never a credential.
    """
    if access.secret is None:
        return (
            f"The package index {access.name!r} asked for a credential, and none is registered "
            "for it. An administrator binds one with "
            f"`lakana index update {shlex.quote(access.name)} --credential-secret NAME` and "
            f"creates the secret NAME in environment {access.environment!r}."
        )
    return (
        f"The package index {access.name!r} refused the credential in secret {access.secret!r} "
        f"of environment {access.environment!r}. Set it again with "
        f"`lakana secret rotate {shlex.quote(access.secret)} --env "
        f"{shlex.quote(access.environment)} --values-stdin`. If the password is right, check "
        "that its account may read the package: the index also answers 403 when the account "
        "may not read a package or file."
    )


def no_wheel_message(package: str) -> str:
    """Say that a package has no usable wheel, and what to do.

    Parameters
    ----------
    package
        The package's name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"{package} has no wheel for this platform and Python; publish a wheel to your index, "
        "or remove the dependency. A project that uses a package index with a credential "
        "installs wheels only."
    )


def missing_secret_message(index: RegisteredIndex, environment: str) -> str:
    """Say that an index's secret does not exist in an environment, and how to create it.

    Parameters
    ----------
    index
        The registered index; it names a secret.
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"the package index {index.name!r} needs the secret {index.credential_secret!r} in "
        f"environment {environment!r}, which does not exist. Create it with "
        f"`lakana secret create {shlex.quote(plain(index.credential_secret or ''))} --env "
        f"{shlex.quote(plain(environment))} --index {shlex.quote(plain(index.name))} "
        '--values-stdin` (a JSON object with "username" and '
        '"password")'
    )


def no_password_message(index: RegisteredIndex, environment: str) -> str:
    """Say that an index's secret holds no usable password, and how to set it again.

    Parameters
    ----------
    index
        The registered index; it names a secret.
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"the secret {index.credential_secret!r} of environment {environment!r}, which the "
        f'package index {index.name!r} uses, has no usable "password". Set it again with '
        f"`lakana secret rotate {shlex.quote(index.credential_secret or '')} --env "
        f'{shlex.quote(environment)} --values-stdin` (a JSON object with "username" and '
        '"password", neither with a NUL character)'
    )


def unencodable_message(index: RegisteredIndex, environment: str) -> str:
    """Say that an index's secret holds a character that cannot be sent, and how to fix it.

    Parameters
    ----------
    index
        The registered index; it names a secret.
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"the secret {index.credential_secret!r} of environment {environment!r}, which the "
        f"package index {index.name!r} uses, holds a user name or password with a character that "
        "cannot be sent (a lone surrogate). Set it again with "
        f"`lakana secret rotate {shlex.quote(index.credential_secret or '')} --env "
        f'{shlex.quote(environment)} --values-stdin` (a JSON object with "username" and '
        '"password")'
    )


def invalid_url_message(index: RegisteredIndex) -> str:
    """Say that an index's stored URL fails the registration rule, and who sets it again.

    Parameters
    ----------
    index
        The registered index as read from the database.

    Returns
    -------
    str
        The message; it holds the name, escaped, and never the stored URL, which may be
        anything.
    """
    return (
        f"the stored URL of the package index {index.name!r} is not valid any more, so no "
        "credential is sent to it. An administrator registers it again with "
        f"`lakana index update {shlex.quote(plain(index.name))} --url URL`"
    )


def credential_not_taken_message(index: RegisteredIndex) -> str:
    """Say that a credential is bound to a host that takes none, and who removes it.

    Parameters
    ----------
    index
        The registered index as read from the database; its URL is valid without a credential.

    Returns
    -------
    str
        The message; it holds the name, escaped, and never the stored URL.
    """
    return (
        f"the package index {index.name!r} has a credential, but its host takes none, so no "
        "credential is sent to it. An administrator removes it with "
        f"`lakana index update {shlex.quote(plain(index.name))} --clear credential_secret`"
    )


def too_long_message(index: RegisteredIndex, environment: str, keys: Sequence[str]) -> str:
    """Say that an index's secret holds a user name or password over the bound.

    Parameters
    ----------
    index
        The registered index; it names a secret.
    environment
        The environment's name.
    keys
        Which of ``username`` and ``password`` are too long.

    Returns
    -------
    str
        The message.
    """
    which = " and a ".join(f'"{key}"' for key in keys)
    return (
        f"the secret {index.credential_secret!r} of environment {environment!r}, which the "
        f"package index {index.name!r} uses, has a {which} longer than "
        f"{MAX_CREDENTIAL_CHARS} characters. Set it again with "
        f"`lakana secret rotate {shlex.quote(index.credential_secret or '')} --env "
        f'{shlex.quote(environment)} --values-stdin` (a JSON object with "username" and '
        '"password")'
    )


def together_too_long_message(environment: str) -> str:
    """Say that the credentials of a project's indexes are too long together.

    Parameters
    ----------
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    return (
        f"the credentials of the package indexes this project uses, in environment "
        f"{environment!r}, are too long together to pass to the build. Shorten them with "
        "`lakana secret rotate NAME --env ENV --values-stdin`, or use fewer indexes with a "
        "credential."
    )


def _stored_url_is_valid(index: RegisteredIndex, credential: bool = True) -> bool:
    """Check a stored URL against the registration rule again.

    Parameters
    ----------
    index
        A registered index as read from the database.
    credential
        Check the URL as one that has a credential (the default) or as one that has none.

    Returns
    -------
    bool
        `True` when `normalize_index_url` accepts the URL for an index with a credential
        (or without, for `credential=False`) and leaves it as stored. A row written
        straight into the database may be anything, one of the seven hosts of the public
        index included.
    """
    try:
        return normalize_index_url(index.url, credential=credential) == index.url
    except ValidationError:
        return False


def resolve_index_access(
    session: Session,
    key_provider: KeyProvider | None,
    environment_id: uuid.UUID,
    environment_name: str,
    indexes: Sequence[RegisteredIndex],
) -> list[IndexAccess]:
    """Decrypt the credentials of the registered indexes a lock uses.

    The third place that decrypts a secret. The plaintext lives only in the
    returned objects; a caller that only checks (the deploy) drops them.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Unwraps the secrets' data keys.
    environment_id
        The environment's id; its secret scope, not any other, is searched.
        Plain values rather than the row, which a commit expires.
    environment_name
        The environment's name, for messages and as the secret's context.
    indexes
        The registered indexes the lock uses (`used_indexes`).

    Returns
    -------
    list of IndexAccess
        One per index, in the given order.

    Raises
    ------
    IndexCredentialError
        If the stored URL of an index with a credential fails the
        registration rule (no secret is read then), no key provider is
        configured, a secret does not exist (or is revoked) in this
        environment, is not bound to this index (`require_use`), the index has no id, cannot be
        decrypted, has no ``password`` that can be passed on, or holds a user name or password
        over `MAX_CREDENTIAL_CHARS`, or the credentials together would make a `UV_INDEX` over
        `MAX_UV_INDEX_BYTES`.
    """
    accesses: list[IndexAccess] = []
    for index in indexes:
        if index.credential_secret is None:
            accesses.append(IndexAccess(index.name, index.url, environment_name))
            continue
        if not _stored_url_is_valid(index):
            raise IndexCredentialError(
                credential_not_taken_message(index)
                if _stored_url_is_valid(index, credential=False)
                else invalid_url_message(index)
            )
        if key_provider is None:
            raise IndexCredentialError(
                f"the package index {index.name!r} has a credential but no secrets master key "
                "is configured. Set `LAKANA_SECRETS__MASTER_KEY` for the server and the worker"
            )
        secret = session.execute(
            select(Secret).where(
                Secret.environment_id == environment_id,
                Secret.name == index.credential_secret,
                Secret.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if secret is None:
            raise IndexCredentialError(missing_secret_message(index, environment_name))
        if index.id is None:
            raise IndexCredentialError(
                f"the package index {index.name!r} has no id, so no secret can be bound to it; "
                "an index read from the registry always has one"
            )
        try:
            require_use(
                session,
                secret,
                SecretUse(SecretUseKind.PACKAGE_INDEX, index.id, index.name),
                environment_name=environment_name,
            )
        except SecretUseRefused as exc:
            raise IndexCredentialError(str(exc)) from exc
        try:
            values = decrypt_secret_row(
                secret,
                key_provider,
                environment_name=environment_name,
                accept_format_1=key_provider.accepts_format_1,
            )
        except LakanaError as exc:
            raise IndexCredentialError(str(exc)) from exc
        password, username = values.get("password"), values.get("username", "")
        usable = (
            isinstance(password, str)
            and bool(password)
            and isinstance(username, str)
            and "\x00" not in password + username
        )
        if not usable:
            raise IndexCredentialError(no_password_message(index, environment_name))
        assert isinstance(password, str) and isinstance(username, str)  # nosec B101 -- `usable`
        too_long = [
            key
            for key, value in (("username", username), ("password", password))
            if len(value) > MAX_CREDENTIAL_CHARS
        ]
        if too_long:
            raise IndexCredentialError(too_long_message(index, environment_name, too_long))
        if not (encodable(password) and encodable(username)):
            raise IndexCredentialError(unencodable_message(index, environment_name))
        accesses.append(
            IndexAccess(
                index.name,
                index.url,
                environment_name,
                secret=index.credential_secret,
                username=username,
                password=password,
            )
        )
    if len((uv_index_value(None, accesses) or "").encode()) > MAX_UV_INDEX_BYTES:
        raise IndexCredentialError(together_too_long_message(environment_name))
    return accesses
