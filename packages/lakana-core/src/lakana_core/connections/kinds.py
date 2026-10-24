"""What each kind of connection holds: its address, its settings and the keys of its secret.

A connection is stored as a kind, an address and settings (both JSON) and at most one bound
secret. One pydantic model per kind checks each part and refuses unknown keys, so a typo is an
error and never an ignored setting. The REST API, the CLI, the deploy check and the worker all
go through `validate_connection`; there is no second copy of these rules.

A refusal names the field and never repeats the value: a value in the wrong place may be a
password.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as ModelError

from lakana_core.errors import ValidationError
from lakana_core.secrets.forms import require_text


class ConnectionKind(enum.StrEnum):
    """What a connection reaches; the database stores member names, REST the values."""

    POSTGRESQL = "postgresql"
    MARIADB = "mariadb"
    SQLITE_FILE = "sqlite-file"
    HTTP = "http"
    S3 = "s3"
    GCS = "gcs"
    AZURE = "azure"
    DIRECTORY = "directory"
    TCP = "tcp"


class _Refusal(ValueError):
    """A rule's own refusal; only these sentences reach a caller, never a library's text."""


NAME = re.compile(r"\A[a-z][a-z0-9-]{0,62}\Z")
"""A connection's name, which code uses to refer to it."""
ROOT_NAME = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,62}\Z")
"""An ingestion root's name: the launcher's pattern."""
_HOST = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
"""The egress token's spelling of a host; a host it could not carry is refused at create."""
_PORT = re.compile(r"\A[0-9]{1,5}\Z")
_BUCKET = re.compile(r"\A[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_GCS_BUCKET = re.compile(r"\A[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]\Z")
_REGION = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_ACCOUNT = re.compile(r"\A[a-z0-9]{3,24}\Z")
_CONTAINER = re.compile(r"\A(?!.*--)[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
_HEADER_NAME = re.compile(r"\A[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\?#]")
_TCP_KEY = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
_ECHOABLE_FIELD = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
MAX_CA_BYTES = 64 * 1024
"""The largest CA bundle (`ca_certificate`); it is not part of `MAX_JSON_BYTES`."""
MAX_JSON_BYTES = 16 * 1024
"""The most JSON an address and its settings may hold together, the CA bundle left out."""
MAX_SECRET_VALUE_BYTES = 64 * 1024
_ADDRESS_MOVING = ("host", "port", "base_url", "endpoint", "bucket", "account")
"""Address fields that decide which host a bound secret is sent to."""
_SETTINGS_MOVING = ("token_url", "ca_certificate", "api_key_in", "auth")
"""Settings that decide where a bound secret goes, whom TLS trusts, or how it is sent."""
_TLS_STRENGTH = {"verify-full": 2, "require": 1, "disable": 0}


def _kind(kind: ConnectionKind | str) -> ConnectionKind:
    """Read a kind given as a member or as its REST value.

    Parameters
    ----------
    kind
        The kind.

    Returns
    -------
    ConnectionKind
        The member.

    Raises
    ------
    ValidationError
        For a kind Lakana does not know, naming the ones it does.
    """
    try:
        return ConnectionKind(kind)
    except ValueError:
        known = ", ".join(member.value for member in ConnectionKind)
        raise ValidationError(f"That is not a connection kind; the kinds are {known}.") from None


def _host(value: str) -> str:
    """Check a host's spelling.

    Parameters
    ----------
    value
        The host.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    _Refusal
        If it is not a lower-case name or IPv4 address without a trailing dot.
    """
    if _HOST.match(value) is None:
        raise _Refusal(
            "must be a lower-case host name or IPv4 address without a trailing dot, "
            "such as db.example.com"
        )
    return value


def _no_control(value: str) -> str:
    """Refuse control characters, which could split a header or a connection string.

    Parameters
    ----------
    value
        The text.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    _Refusal
        For a control character.
    """
    if _CONTROL.search(value):
        raise _Refusal("must not contain control characters")
    return value


def _https(value: str) -> str:
    """Check an https URL that carries no credential.

    `urlsplit` silently drops tabs and newlines and strips leading blanks, and a client may
    read a backslash as a slash, so those are refused outright: the host checked here must be
    the host every client connects to.

    Parameters
    ----------
    value
        The URL.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    _Refusal
        For another scheme, a blank, control character or backslash, a user name or password,
        a query, a fragment, a host the egress token could not carry, or a port outside 1 to
        65535.
    """
    if not value.isascii():
        raise _Refusal(
            "must be ASCII: percent-encode other characters in the path, and give a host "
            "in its xn-- (punycode) form"
        )
    if not value.startswith("https://"):
        raise _Refusal("must be an https:// URL: Lakana sends credentials only over TLS")
    if "@" in value.partition("//")[2].partition("/")[0]:
        raise _Refusal(
            "must not contain a user name or password; "
            "put the credential in the connection's secret"
        )
    if _URL_FORBIDDEN.search(value):
        raise _Refusal(
            "must not contain a query, a fragment, blanks, control characters or backslashes"
        )
    try:
        netloc = urlsplit(value).netloc
    except ValueError:  # its text quotes the URL, which may carry a credential
        raise _Refusal("is not a URL Lakana can read") from None
    host, colon, port = netloc.partition(":")
    _host(host)
    if colon and (_PORT.match(port) is None or not 0 < int(port) < 65536):
        raise _Refusal("has a port that is not a number from 1 to 65535")
    return value


def _relative(value: str, *, allow_empty: bool = False) -> str:
    """Check a path below a root or a prefix: relative, no `..`, no NUL.

    Parameters
    ----------
    value
        The path.
    allow_empty
        Whether an empty path (the root itself, for a prefix) is accepted.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    _Refusal
        For an absolute path, a `..` segment, a NUL or another control character, a backslash,
        more than 1024 characters, or an empty path where one is needed.
    """
    if not value and not allow_empty:
        raise _Refusal("must name a path below the root, such as exports or . for the root itself")
    if len(value) > 1024 or _CONTROL.search(value) or "\\" in value:
        raise _Refusal("must be at most 1024 characters, without control characters or backslashes")
    if value.startswith("/") or ".." in value.split("/"):
        raise _Refusal("must be relative and may not contain a .. segment")
    return value


def _certificate(value: str) -> str:
    """Check a CA bundle: PEM certificates and nothing else, at most 64 KiB.

    Parameters
    ----------
    value
        The PEM text.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    _Refusal
        If it is larger than 64 KiB, not one or more PEM certificates, or holds another PEM
        block (a private key pasted with the certificate would be stored as a plain setting).
    """
    if len(value) > MAX_CA_BYTES or len(value.encode("utf-8")) > MAX_CA_BYTES:
        raise _Refusal("must be at most 64 KiB")
    if value.count("-----BEGIN ") != value.count("-----BEGIN CERTIFICATE-----"):
        raise _Refusal(
            "must hold only certificates (-----BEGIN CERTIFICATE-----); remove any key or "
            "other block, and keep private keys out of a connection's settings"
        )
    try:
        found = x509.load_pem_x509_certificates(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        found = []
    if not found:
        raise _Refusal("must be one or more PEM certificates (-----BEGIN CERTIFICATE-----)")
    return value


def fingerprint(pem: str) -> str:
    """Show a CA bundle by its certificates' SHA-256 fingerprints.

    Parameters
    ----------
    pem
        A bundle `_certificate` accepted.

    Returns
    -------
    str
        ``sha256:<hex>`` per certificate, joined by ``, ``.
    """
    return ", ".join(
        "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
        for certificate in x509.load_pem_x509_certificates(pem.encode("ascii"))
    )


class _Part(BaseModel):
    """One part (address or settings) of one kind; unknown keys are refused."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _DatabaseAddress(_Part):
    """A database server: host, port, database."""

    DEFAULT_PORT: ClassVar[int] = 5432
    host: str
    port: int = Field(default=0, ge=0, le=65535)
    database: str = Field(min_length=1, max_length=255)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        """Check the host.

        Parameters
        ----------
        value
            The host.

        Returns
        -------
        str
            `value`.
        """
        return _host(value)

    @field_validator("database")
    @classmethod
    def _check_database(cls, value: str) -> str:
        """Refuse control characters in the database name.

        Parameters
        ----------
        value
            The name.

        Returns
        -------
        str
            `value`.
        """
        return _no_control(value)

    @model_validator(mode="after")
    def _default_port(self) -> _DatabaseAddress:
        """Fill the kind's port, and refuse port 0 given explicitly.

        Returns
        -------
        _DatabaseAddress
            The address with its port.

        Raises
        ------
        _Refusal
            For an explicit port 0.
        """
        if "port" in self.model_fields_set and self.port == 0:
            raise _Refusal("port must be a number from 1 to 65535")
        if self.port == 0:
            object.__setattr__(self, "port", self.DEFAULT_PORT)
        return self


class _PostgresAddress(_DatabaseAddress):
    """A PostgreSQL server (default port 5432)."""

    DEFAULT_PORT: ClassVar[int] = 5432


class _MariadbAddress(_DatabaseAddress):
    """A MariaDB or MySQL server (default port 3306)."""

    DEFAULT_PORT: ClassVar[int] = 3306


class _TcpAddress(_Part):
    """A host and port speaking a protocol Lakana does not know; the team's own driver reads it.

    No TLS setting: Lakana cannot see inside the protocol, so TLS is the driver's business.
    """

    host: str
    port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        """Check the host.

        Parameters
        ----------
        value
            The host.

        Returns
        -------
        str
            `value`.
        """
        return _host(value)


class _CaSettings(_Part):
    """Settings that may carry the team's own CA certificates (PEM)."""

    ca_certificate: str | None = None

    @field_validator("ca_certificate")
    @classmethod
    def _check_ca(cls, value: str | None) -> str | None:
        """Check the CA bundle.

        Parameters
        ----------
        value
            The PEM text, or `None`.

        Returns
        -------
        str or None
            `value`.
        """
        return None if value is None else _certificate(value)


class _DatabaseSettings(_CaSettings):
    """Who logs in, and how TLS is checked."""

    user: str = Field(min_length=1, max_length=255)
    tls: Literal["verify-full", "require", "disable"] = "verify-full"

    @field_validator("user")
    @classmethod
    def _check_user(cls, value: str) -> str:
        """Refuse control characters in the user name.

        Parameters
        ----------
        value
            The user.

        Returns
        -------
        str
            `value`.
        """
        return _no_control(value)


class _FileAddress(_Part):
    """A root the operator configured, and a path below it."""

    root: str
    path: str

    @field_validator("root")
    @classmethod
    def _check_root(cls, value: str) -> str:
        """Check the root's name.

        Parameters
        ----------
        value
            The name.

        Returns
        -------
        str
            `value`.

        Raises
        ------
        _Refusal
            If it is not a root name.
        """
        if ROOT_NAME.match(value) is None:
            raise _Refusal(
                "must be the name of an ingestion root: lower-case letters, digits, '-' and '_'"
            )
        return value

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        """Check the path below the root.

        Parameters
        ----------
        value
            The path.

        Returns
        -------
        str
            `value`.
        """
        return _relative(value)


class _NoSettings(_Part):
    """A kind without settings."""


class _HttpAddress(_Part):
    """An HTTPS API's base URL."""

    base_url: str

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        """Check the URL.

        Parameters
        ----------
        value
            The URL.

        Returns
        -------
        str
            `value`.
        """
        return _https(value)


_AUTH_FIELDS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "bearer": frozenset(),
    "basic": frozenset({"username"}),
    "api_key": frozenset({"api_key_name", "api_key_in"}),
    "oauth2": frozenset({"client_id", "token_url", "scope"}),
}
_AUTH_REQUIRED: dict[str, tuple[str, ...]] = {
    "basic": ("username",),
    "api_key": ("api_key_name",),
    "oauth2": ("client_id", "token_url"),
}
_AUTH_ONLY_FIELDS = sorted(frozenset().union(*_AUTH_FIELDS.values()))


class _HttpSettings(_CaSettings):
    """How an HTTPS API authenticates."""

    auth: Literal["none", "bearer", "basic", "api_key", "oauth2"] = "none"
    username: str | None = Field(default=None, min_length=1, max_length=255)
    api_key_name: str | None = None
    api_key_in: Literal["header", "query"] | None = None
    client_id: str | None = Field(default=None, min_length=1, max_length=512)
    token_url: str | None = None
    scope: str | None = Field(default=None, max_length=1024)

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str | None) -> str | None:
        """Check a basic-auth user name: no control character, no colon (RFC 7617).

        Parameters
        ----------
        value
            The user name, or `None`.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        _Refusal
            For a control character or a colon.
        """
        if value is not None and (":" in value or _CONTROL.search(value)):
            raise _Refusal("must not contain a colon or control characters")
        return value

    @field_validator("api_key_name")
    @classmethod
    def _check_api_key_name(cls, value: str | None) -> str | None:
        """Check the API key's header or query parameter name.

        Parameters
        ----------
        value
            The name, or `None`.

        Returns
        -------
        str or None
            `value`.

        Raises
        ------
        _Refusal
            If it is not an HTTP header name.
        """
        if value is not None and _HEADER_NAME.match(value) is None:
            raise _Refusal(
                "must be a header name of at most 128 letters, digits and '-', such as X-API-Key"
            )
        return value

    @field_validator("client_id", "scope")
    @classmethod
    def _check_text(cls, value: str | None) -> str | None:
        """Refuse control characters in the OAuth2 client id and scope.

        Parameters
        ----------
        value
            The text, or `None`.

        Returns
        -------
        str or None
            `value`.
        """
        return None if value is None else _no_control(value)

    @field_validator("token_url")
    @classmethod
    def _check_token_url(cls, value: str | None) -> str | None:
        """Check the token URL.

        Parameters
        ----------
        value
            The URL, or `None`.

        Returns
        -------
        str or None
            `value`.
        """
        return None if value is None else _https(value)

    @model_validator(mode="after")
    def _fields_of_the_auth(self) -> _HttpSettings:
        """Require the auth's own fields and refuse another auth's.

        Returns
        -------
        _HttpSettings
            The settings, `api_key_in` defaulted to ``header`` for an API key.

        Raises
        ------
        _Refusal
            Naming the missing or the misplaced field.
        """
        for name in _AUTH_REQUIRED.get(self.auth, ()):
            if getattr(self, name) is None:
                raise _Refusal(f"{name} is required with auth {self.auth}")
        allowed = _AUTH_FIELDS[self.auth]
        for name in _AUTH_ONLY_FIELDS:
            if name not in allowed and getattr(self, name) is not None:
                owners = ", ".join(a for a, f in _AUTH_FIELDS.items() if name in f)
                raise _Refusal(f"{name} applies to auth {owners} only, not to {self.auth}")
        if self.auth == "api_key" and self.api_key_in is None:
            object.__setattr__(self, "api_key_in", "header")
        return self


class _S3Address(_Part):
    """An S3 bucket, its region, and the endpoint that serves it."""

    bucket: str
    region: str
    endpoint: str | None = None

    @field_validator("bucket")
    @classmethod
    def _check_bucket(cls, value: str) -> str:
        """Check the bucket name.

        Parameters
        ----------
        value
            The name.

        Returns
        -------
        str
            `value`.

        Raises
        ------
        _Refusal
            If it is not an S3 bucket name.
        """
        if _BUCKET.match(value) is None:
            raise _Refusal(
                "must be an S3 bucket name: 3 to 63 lower-case letters, digits, '.' and '-'"
            )
        return value

    @field_validator("region")
    @classmethod
    def _check_region(cls, value: str) -> str:
        """Check the region; the default endpoint's host is built from it.

        Parameters
        ----------
        value
            The region.

        Returns
        -------
        str
            `value`.

        Raises
        ------
        _Refusal
            If it is not a region name.
        """
        if _REGION.match(value) is None:
            raise _Refusal("must be a region name such as eu-central-1")
        return value

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str | None) -> str | None:
        """Check the endpoint.

        Parameters
        ----------
        value
            The URL, or `None` for AWS.

        Returns
        -------
        str or None
            `value`.
        """
        return None if value is None else _https(value)


class _PrefixSettings(_Part):
    """An optional prefix inside a bucket or container."""

    prefix: str | None = None

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, value: str | None) -> str | None:
        """Check the prefix.

        Parameters
        ----------
        value
            The prefix, or `None`.

        Returns
        -------
        str or None
            `value`.
        """
        return None if value is None else _relative(value, allow_empty=True)


class _S3Settings(_PrefixSettings):
    """S3 credentials' public half.

    No CA setting: an endpoint with a private CA is trusted through the operator's trust
    bundle (`LAKANA_RUN__CA_BUNDLE`), which obstore reads.
    """

    access_key_id: str = Field(min_length=1, max_length=256)

    @field_validator("access_key_id")
    @classmethod
    def _check_key_id(cls, value: str) -> str:
        """Refuse control characters in the access key id.

        Parameters
        ----------
        value
            The id.

        Returns
        -------
        str
            `value`.
        """
        return _no_control(value)


class _GcsAddress(_Part):
    """A GCS bucket."""

    bucket: str

    @field_validator("bucket")
    @classmethod
    def _check_bucket(cls, value: str) -> str:
        """Check the bucket name.

        Parameters
        ----------
        value
            The name.

        Returns
        -------
        str
            `value`.

        Raises
        ------
        _Refusal
            If it is not a GCS bucket name.
        """
        if _GCS_BUCKET.match(value) is None:
            raise _Refusal(
                "must be a GCS bucket name: lower-case letters, digits, '.', '_' and '-'"
            )
        return value


class _AzureAddress(_Part):
    """An Azure storage account and a container in it."""

    account: str
    container: str

    @field_validator("account")
    @classmethod
    def _check_account(cls, value: str) -> str:
        """Check the account name.

        Parameters
        ----------
        value
            The name.

        Returns
        -------
        str
            `value`.

        Raises
        ------
        _Refusal
            If it is not an account name.
        """
        if _ACCOUNT.match(value) is None:
            raise _Refusal("must be a storage account name: 3 to 24 lower-case letters and digits")
        return value

    @field_validator("container")
    @classmethod
    def _check_container(cls, value: str) -> str:
        """Check the container name.

        Parameters
        ----------
        value
            The name.

        Returns
        -------
        str
            `value`.

        Raises
        ------
        _Refusal
            If it is not a container name.
        """
        if _CONTAINER.match(value) is None:
            raise _Refusal(
                "must be a container name: 3 to 63 lower-case letters, digits and single '-'"
            )
        return value


_MODELS: dict[ConnectionKind, tuple[type[_Part], type[_Part]]] = {
    ConnectionKind.POSTGRESQL: (_PostgresAddress, _DatabaseSettings),
    ConnectionKind.MARIADB: (_MariadbAddress, _DatabaseSettings),
    ConnectionKind.SQLITE_FILE: (_FileAddress, _NoSettings),
    ConnectionKind.HTTP: (_HttpAddress, _HttpSettings),
    ConnectionKind.S3: (_S3Address, _S3Settings),
    ConnectionKind.GCS: (_GcsAddress, _PrefixSettings),
    ConnectionKind.AZURE: (_AzureAddress, _PrefixSettings),
    ConnectionKind.DIRECTORY: (_FileAddress, _NoSettings),
    ConnectionKind.TCP: (_TcpAddress, _NoSettings),
}


def _problem(kind: ConnectionKind, part: str, model: type[_Part], error: ModelError) -> str:
    """Say what is wrong with a part, naming the field and never the value.

    An unknown field is reported before anything else, so a typo (``hots``) is named as
    itself rather than as the field it was meant to be ("host is missing").

    Parameters
    ----------
    kind
        The kind.
    part
        ``address`` or ``settings``.
    model
        The part's model, for the list of its fields.
    error
        pydantic's error.

    Returns
    -------
    str
        One sentence without its full stop.
    """
    errors = error.errors()
    first = next((e for e in errors if e["type"] == "extra_forbidden"), errors[0])
    field = ".".join(str(item) for item in first["loc"])
    if first["type"] == "extra_forbidden":
        fields = ", ".join(model.model_fields) or "none"
        if _ECHOABLE_FIELD.match(field) is None:
            return (
                f"{part} holds a key that is not a field of kind {kind.value}; "
                f"its fields are {fields}"
            )
        return f"{part}.{field} is not a field of kind {kind.value}; its fields are {fields}"
    if first["type"] == "missing":
        return f"{part}.{field} is required for kind {kind.value}"
    if first["type"] == "value_error" and not isinstance(
        first.get("ctx", {}).get("error"), _Refusal
    ):
        return f"{part}.{field or 'value'} is not valid for kind {kind.value}"
    message = str(first["msg"]).removeprefix("Value error, ")
    if not field:  # a model validator's message starts with the field's name
        return f"{part}.{message}"
    return f"{part}.{field} {message}"


def _json_size(values: Mapping[str, Any]) -> int:
    """Measure a part as JSON, a text `ca_certificate` left out (it has its own limit).

    Parameters
    ----------
    values
        The address or the settings, as sent.

    Returns
    -------
    int
        Bytes of compact-enough JSON.

    Raises
    ------
    ValidationError
        If the part cannot be written as JSON.
    """
    counted = {
        key: value
        for key, value in values.items()
        if not (key == "ca_certificate" and isinstance(value, str))
    }
    try:
        return len(json.dumps(counted).encode())
    except (TypeError, ValueError):
        raise ValidationError(
            "A connection's address and settings must be JSON objects of text, numbers and "
            "booleans."
        ) from None


def s3_default_endpoint(region: str) -> str:
    """Give the AWS endpoint of a region, the default of an `s3` connection's `endpoint`.

    Parameters
    ----------
    region
        The region.

    Returns
    -------
    str
        ``https://s3.<region>.amazonaws.com``.
    """
    return f"https://s3.{region}.amazonaws.com"


def merge_part(stored: Mapping[str, Any], changes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Lay changes over a stored address or settings; a key given as `None` is removed.

    Parameters
    ----------
    stored
        The stored address or settings.
    changes
        The keys to change, or `None` for none.

    Returns
    -------
    dict
        The part after the change, not yet validated.
    """
    merged = dict(stored)
    for key, value in (changes or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def follow_region(
    kind: ConnectionKind, before: Mapping[str, Any], changes: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Lay address changes over a stored address, moving a default `s3` endpoint with its region.

    An `s3` endpoint that is the default of the stored region was derived from it, so a new
    `region` without an `endpoint` derives the endpoint again; a custom endpoint, or one given
    in the same change, stays. A key changed to `None` is removed.

    Parameters
    ----------
    kind
        The kind.
    before
        The stored address.
    changes
        The keys to change, or `None` for none.

    Returns
    -------
    dict
        The address after the change, not yet validated.
    """
    merged = merge_part(before, changes)
    if (
        _kind(kind) is ConnectionKind.S3
        and changes
        and "region" in changes
        and "endpoint" not in changes
        and before.get("endpoint") == s3_default_endpoint(str(before.get("region")))
    ):
        merged.pop("endpoint", None)
    return merged


def validate_connection(
    kind: ConnectionKind | str, address: Mapping[str, Any], settings: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check a connection's address and settings for its kind.

    Parameters
    ----------
    kind
        The kind (a member or its REST value).
    address
        The address as sent.
    settings
        The settings as sent.

    Returns
    -------
    tuple of (dict, dict)
        Address and settings with their defaults filled and `None` values dropped; an `s3`
        address always has an `endpoint`.

    Raises
    ------
    ValidationError
        Naming the first field that breaks its rule (``address.host``, ``settings.user``), or
        the size limit of both parts together (16 KiB, the CA bundle counted on its own).
    """
    kind = _kind(kind)
    size = _json_size(address) + _json_size(settings)
    if size > MAX_JSON_BYTES:
        raise ValidationError(
            "A connection's address and settings may hold at most 16 KiB of JSON together, "
            f"not counting ca_certificate ({size} bytes)."
        )
    address_model, settings_model = _MODELS[kind]
    checked: list[dict[str, Any]] = []
    for part, model, values in (
        ("address", address_model, address),
        ("settings", settings_model, settings),
    ):
        try:
            checked.append(model.model_validate(dict(values)).model_dump(exclude_none=True))
        except ModelError as exc:
            raise ValidationError(_problem(kind, part, model, exc) + ".") from None
    new_address, new_settings = checked
    if kind is ConnectionKind.S3 and "endpoint" not in new_address:
        new_address["endpoint"] = s3_default_endpoint(new_address["region"])
    return new_address, new_settings


def secret_keys(
    kind: ConnectionKind, settings: Mapping[str, Any]
) -> tuple[frozenset[str], frozenset[str]]:
    """Name the keys a connection's secret must and may hold.

    Parameters
    ----------
    kind
        The kind.
    settings
        Validated settings (the `http` auth decides).

    Returns
    -------
    tuple of (frozenset, frozenset)
        Required keys, optional keys. `azure` requires exactly one of its two optional keys
        (`check_secret_values` enforces it); `tcp` returns two empty sets because its keys
        are the team's own.
    """
    kind = _kind(kind)
    if kind in (ConnectionKind.POSTGRESQL, ConnectionKind.MARIADB):
        return frozenset({"password"}), frozenset()
    if kind is ConnectionKind.HTTP:
        key = {
            "bearer": "token",
            "basic": "password",
            "api_key": "api_key",
            "oauth2": "client_secret",
        }
        auth = str(settings.get("auth", "none"))
        return (frozenset({key[auth]}) if auth in key else frozenset()), frozenset()
    if kind is ConnectionKind.S3:
        return frozenset({"secret_access_key"}), frozenset({"session_token"})
    if kind is ConnectionKind.GCS:
        return frozenset({"service_account_json"}), frozenset()
    if kind is ConnectionKind.AZURE:
        return frozenset(), frozenset({"account_key", "sas_token"})
    return frozenset(), frozenset()


def takes_secret(kind: ConnectionKind, settings: Mapping[str, Any]) -> bool:
    """Say whether a connection of this kind and these settings sends a secret.

    Parameters
    ----------
    kind
        The kind.
    settings
        Validated settings (the `http` auth decides).

    Returns
    -------
    bool
        Whether it takes one; `tcp` always may.
    """
    kind = _kind(kind)
    required, optional = secret_keys(kind, settings)
    return kind is ConnectionKind.TCP or bool(required | optional)


def single_secret_key(kind: ConnectionKind, settings: Mapping[str, Any]) -> str | None:
    """Name the key `--password-stdin` stores stdin under.

    Parameters
    ----------
    kind
        The kind.
    settings
        Validated settings.

    Returns
    -------
    str or None
        The key; `None` for a kind or auth without a secret.
    """
    kind = _kind(kind)
    if kind is ConnectionKind.AZURE:
        return "account_key"
    if kind is ConnectionKind.TCP:
        return "password"
    required, _ = secret_keys(kind, settings)
    return next(iter(required), None)


def _check_value(key: str, value: object) -> None:
    """Check one secret value's shape, never its content.

    Parameters
    ----------
    key
        Its key, for the message.
    value
        The value.

    Raises
    ------
    ValidationError
        For a value that is not text (also one that cannot be written as UTF-8), is empty, or
        is larger than 64 KiB.
    """
    if isinstance(value, str):
        require_text({key: value})
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SECRET_VALUE_BYTES
        or len(value.encode()) > MAX_SECRET_VALUE_BYTES
    ):
        raise ValidationError(f"The secret's {key} must be a non-empty text of at most 64 KiB.")


def check_secret_values(
    kind: ConnectionKind, settings: Mapping[str, Any], values: Mapping[str, str]
) -> None:
    """Check the keys of a connection's secret, never its values' content.

    Parameters
    ----------
    kind
        The kind.
    settings
        Validated settings.
    values
        Key → value.

    Raises
    ------
    ValidationError
        For a kind without a secret, an unknown key, a missing required key, `azure` without
        exactly one key, a `tcp` key outside its pattern, or an empty value or one larger than
        64 KiB; messages name keys only, and a key that does not look like one is not echoed.
    """
    kind = _kind(kind)
    if kind is ConnectionKind.TCP:
        if not values or len(values) > 16 or not all(_TCP_KEY.match(key) for key in values):
            raise ValidationError(
                "The secret keys of a tcp connection are 1 to 16 names of lower-case letters, "
                "digits and '_', such as user and password."
            )
        for key in sorted(values):
            _check_value(key, values[key])
        return
    required, optional = secret_keys(kind, settings)
    allowed = required | optional
    if not allowed:
        if kind is ConnectionKind.HTTP:
            raise ValidationError(
                f"An http connection with auth {settings.get('auth', 'none')} takes no secret; "
                "set settings.auth to bearer, basic, api_key or oauth2 first, or leave out the "
                "secret."
            )
        raise ValidationError(f"A connection of kind {kind.value} takes no secret.")
    keys = ", ".join(sorted(allowed))
    for key in sorted(values):
        if key not in allowed:
            if _TCP_KEY.match(key) is None:
                raise ValidationError(
                    f"The secret holds a key that is not a secret key of kind {kind.value}; "
                    f"its keys are {keys}."
                )
            raise ValidationError(
                f"{key} is not a secret key of kind {kind.value}; its keys are {keys}."
            )
        _check_value(key, values[key])
    missing = sorted(required - set(values))
    if missing:
        raise ValidationError(
            f"The secret of a {kind.value} connection needs {', '.join(missing)}."
        )
    if kind is ConnectionKind.AZURE and len(set(values) & optional) != 1:
        raise ValidationError(
            "The secret of an azure connection holds exactly one of account_key, sas_token."
        )


def moves_secret(
    kind: ConnectionKind,
    old_address: Mapping[str, Any],
    old_settings: Mapping[str, Any],
    new_address: Mapping[str, Any],
    new_settings: Mapping[str, Any],
) -> bool:
    """Say whether a change could expose a bound secret, so it must be re-supplied or managed.

    True when the secret would go to another place (``host``, ``port``, ``base_url``,
    ``endpoint``, ``token_url``, ``bucket``, ``account``), when TLS would trust differently
    (``tls`` weakened, ``ca_certificate`` set, changed or removed), or when it would be sent
    differently (``auth`` or ``api_key_in`` changed).

    Parameters
    ----------
    kind
        The kind (unused today; kept so a kind can add its own fields).
    old_address
        The address before the change, validated.
    old_settings
        The settings before the change, validated.
    new_address
        The address after the change, validated.
    new_settings
        The settings after the change, validated.

    Returns
    -------
    bool
        Whether the change moves the secret.
    """
    del kind
    if any(old_address.get(f) != new_address.get(f) for f in _ADDRESS_MOVING):
        return True
    if any(old_settings.get(f) != new_settings.get(f) for f in _SETTINGS_MOVING):
        return True
    old_tls = _TLS_STRENGTH.get(str(old_settings.get("tls", "verify-full")), 0)
    new_tls = _TLS_STRENGTH.get(str(new_settings.get("tls", "verify-full")), 0)
    return new_tls < old_tls
