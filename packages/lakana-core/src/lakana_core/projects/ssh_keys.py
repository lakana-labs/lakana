"""SSH public keys and certificates, read as OpenSSH reads them.

`read_key` parses a key blob as OpenSSH's ``sshkey_fromb`` does: every field of the type's wire
format, every length checked and no byte left over; a certificate's own fields as
``cert_parse`` checks them, and its signature verified with the key of the authority that
signed it. A key OpenSSH would refuse reads as `None`. What is kept is the key's type and the
blob of its plain key, written again as OpenSSH writes it: ``ssh-keygen -l`` prints the SHA-256
of that blob, for a certificate too.

Pinned against ssh-keygen by ``test_known_host_keys.py``. Not read: a ``ssh-dss`` key, which
OpenSSH 10 no longer reads either. A certificate signed by a security key (``sk-``), whose
signature has the authenticator's own format, or by an RSA key `cryptography` refuses (an
exponent under 3 or even) cannot be verified here: it is refused unless the caller asks for no
signature check (`read_key`'s ``check_signature``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

CERT_SUFFIX = "-cert-v01@openssh.com"
"""What a certificate's type adds to its key's."""

MAX_BIGNUM_BYTES = 2048
"""The longest number OpenSSH reads (``SSHBUF_MAX_BIGNUM``), with one more for a leading zero."""

RSA_MIN_BITS = 1024
"""The shortest RSA modulus OpenSSH reads (``SSH_RSA_MINIMUM_MODULUS_SIZE``)."""

MAX_PRINCIPALS = 256
"""The most principals OpenSSH reads in a certificate (``SSHKEY_CERT_MAX_PRINCIPALS``)."""

_CERT_TYPES = frozenset({1, 2})
"""A user and a host certificate; OpenSSH refuses any other type."""


class _Refused(Exception):
    """OpenSSH refuses the blob."""


class _Reader:
    """Read the fields of an SSH wire format, each length checked.

    Parameters
    ----------
    data
        The bytes.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.offset = 0

    def _take(self, size: int) -> bytes:
        """Read `size` bytes.

        Parameters
        ----------
        size
            How many.

        Returns
        -------
        bytes
            The bytes.

        Raises
        ------
        _Refused
            When fewer are left.
        """
        end = self.offset + size
        if end > len(self._data):
            raise _Refused
        chunk = self._data[self.offset : end]
        self.offset = end
        return chunk

    def uint32(self) -> int:
        """Read a four-byte number.

        Returns
        -------
        int
            The number.
        """
        return int.from_bytes(self._take(4), "big")

    def uint64(self) -> None:
        """Skip an eight-byte number."""
        self._take(8)

    def string(self) -> bytes:
        """Read a string: a four-byte length and that many bytes.

        Returns
        -------
        bytes
            The bytes.
        """
        return self._take(self.uint32())

    def cstring(self) -> bytes:
        """Read a string OpenSSH keeps as a C string.

        Returns
        -------
        bytes
            The bytes before a NUL that ends the string.

        Raises
        ------
        _Refused
            When a NUL comes before its last byte.
        """
        value = self.string()
        if b"\0" in value[:-1]:
            raise _Refused
        return value.removesuffix(b"\0")

    def mpint(self) -> int:
        """Read a number of zero or more, as ``sshbuf_get_bignum2`` does.

        Returns
        -------
        int
            The number; leading zero bytes are allowed.

        Raises
        ------
        _Refused
            For a negative number, or one longer than `MAX_BIGNUM_BYTES`.
        """
        value = self.string()
        if value[:1] >= b"\x80" or len(value) > MAX_BIGNUM_BYTES + (value[:1] == b"\0"):
            raise _Refused
        return int.from_bytes(value, "big")

    def done(self) -> bool:
        """Tell whether every byte was read.

        Returns
        -------
        bool
            Whether nothing is left.
        """
        return self.offset == len(self._data)

    def end(self) -> None:
        """Check that every byte was read.

        Raises
        ------
        _Refused
            When a byte is left over.
        """
        if not self.done():
            raise _Refused


def _string(data: bytes) -> bytes:
    """Write an SSH string.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    bytes
        A four-byte length and the bytes.
    """
    return len(data).to_bytes(4, "big") + data


def _mpint(value: int) -> bytes:
    """Write a number as OpenSSH writes it: as short as it can be, zero as no bytes.

    Parameters
    ----------
    value
        A number of zero or more.

    Returns
    -------
    bytes
        The number as an SSH string.
    """
    return _string(value.to_bytes((value.bit_length() + 8) // 8, "big") if value else b"")


@dataclass(frozen=True)
class _Curve:
    """A NIST curve OpenSSH signs with.

    Parameters
    ----------
    name
        Its name inside a key blob, such as ``nistp256``.
    curve
        The curve.
    hash
        The hash its signatures use.
    order
        The order of its group.
    """

    name: bytes
    curve: ec.EllipticCurve
    hash: hashes.HashAlgorithm
    order: int


_CURVES = {
    "ecdsa-sha2-nistp256": _Curve(
        b"nistp256",
        ec.SECP256R1(),
        hashes.SHA256(),
        0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
    ),
    "ecdsa-sha2-nistp384": _Curve(
        b"nistp384",
        ec.SECP384R1(),
        hashes.SHA384(),
        int(
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFC7634D81F4372DDF581A0DB248B0A77AECEC196A"
            "CCC52973",
            16,
        ),
    ),
    "ecdsa-sha2-nistp521": _Curve(
        b"nistp521",
        ec.SECP521R1(),
        hashes.SHA512(),
        int(
            "01FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFA51868783BF2F966B7F"
            "CC0148F709A5D03BB5C9B8899C47AEBB6FB71E91386409",
            16,
        ),
    ),
}
"""The ECDSA key types and their curves."""


@dataclass(frozen=True)
class _Plain:
    """A plain key, read.

    Parameters
    ----------
    fields
        Its fields after the type, written again as OpenSSH writes them.
    public
        What its signatures are checked with: the ed25519 key's bytes, the ECDSA point, or
        the RSA key's ``(e, n)``.
    """

    fields: bytes
    public: bytes | tuple[int, int]


def _ed25519(reader: _Reader) -> _Plain:
    """Read an ed25519 key: its 32 bytes.

    Parameters
    ----------
    reader
        The blob after the type.

    Returns
    -------
    _Plain
        The key.

    Raises
    ------
    _Refused
        When the key is not 32 bytes.
    """
    key = reader.string()
    if len(key) != 32:
        raise _Refused
    return _Plain(_string(key), key)


def _ecdsa(kind: str) -> Callable[[_Reader], _Plain]:
    """Read an ECDSA key of one curve: the curve's name and the point.

    Parameters
    ----------
    kind
        The key type, one of `_CURVES`.

    Returns
    -------
    callable
        The reader of that type's fields.
    """
    curve = _CURVES[kind]

    def read(reader: _Reader) -> _Plain:
        """Read the curve's name and an uncompressed point, as ``sshkey_ec_validate_public``.

        Parameters
        ----------
        reader
            The blob after the type.

        Returns
        -------
        _Plain
            The key.

        Raises
        ------
        _Refused
            For another curve, a point that is compressed or off the curve, or a coordinate
            OpenSSH refuses: half the order's bits or fewer, or the order less one or more.
        """
        if reader.cstring() != curve.name:
            raise _Refused
        point = reader.string()
        if point[:1] != b"\x04":
            raise _Refused
        try:
            key = ec.EllipticCurvePublicKey.from_encoded_point(curve.curve, point)
        except ValueError:
            raise _Refused from None
        public = key.public_numbers()
        half = curve.order.bit_length() // 2
        if any(c.bit_length() <= half or c >= curve.order - 1 for c in (public.x, public.y)):
            raise _Refused
        return _Plain(_string(curve.name) + _string(point), point)

    return read


def _rsa(reader: _Reader) -> _Plain:
    """Read an RSA key: the exponent and the modulus.

    Parameters
    ----------
    reader
        The blob after the type.

    Returns
    -------
    _Plain
        The key.

    Raises
    ------
    _Refused
        For a modulus shorter than `RSA_MIN_BITS`.
    """
    e, n = reader.mpint(), reader.mpint()
    if n.bit_length() < RSA_MIN_BITS:
        raise _Refused
    return _Plain(_mpint(e) + _mpint(n), (e, n))


def _security_key(read: Callable[[_Reader], _Plain]) -> Callable[[_Reader], _Plain]:
    """Read a security key: its key's fields, then the application.

    Parameters
    ----------
    read
        The reader of the key's fields.

    Returns
    -------
    callable
        The reader of the security key's fields.
    """

    def read_sk(reader: _Reader) -> _Plain:
        """Read the key's fields and the application.

        Parameters
        ----------
        reader
            The blob after the type.

        Returns
        -------
        _Plain
            The key; its signatures are not checked.
        """
        key = read(reader)
        return _Plain(key.fields + _string(reader.cstring()), b"")

    return read_sk


SK_ED25519 = "sk-ssh-ed25519@openssh.com"
SK_ECDSA = "sk-ecdsa-sha2-nistp256@openssh.com"

_FIELDS: dict[str, Callable[[_Reader], _Plain]] = {
    "ssh-ed25519": _ed25519,
    **{kind: _ecdsa(kind) for kind in _CURVES},
    "ssh-rsa": _rsa,
    SK_ED25519: _security_key(_ed25519),
    SK_ECDSA: _security_key(_ecdsa("ecdsa-sha2-nistp256")),
}
"""The reader of each plain key type's fields."""

_SIGNERS = frozenset(_FIELDS) - {SK_ED25519, SK_ECDSA}
"""The key types whose signatures on a certificate are checked."""


def _certificate_type(kind: str) -> str:
    """Name a key type's certificate.

    Parameters
    ----------
    kind
        A plain key type.

    Returns
    -------
    str
        The certificate's type, such as ``ssh-ed25519-cert-v01@openssh.com``.
    """
    return kind.removesuffix("@openssh.com") + CERT_SUFFIX


_RSA_HASHES: dict[str, Callable[[], hashes.HashAlgorithm]] = {
    "ssh-rsa": hashes.SHA1,
    "rsa-sha2-256": hashes.SHA256,
    "rsa-sha2-512": hashes.SHA512,
}
"""The hash of each RSA signature name; OpenSSH accepts all three on a certificate."""

_NAMES = {
    **{kind: kind for kind in _FIELDS},
    **{_certificate_type(kind): _certificate_type(kind) for kind in _FIELDS},
    **dict.fromkeys(_RSA_HASHES, "ssh-rsa"),
    **{_certificate_type(alias): _certificate_type("ssh-rsa") for alias in _RSA_HASHES},
    "webauthn-sk-ecdsa-sha2-nistp256@openssh.com": SK_ECDSA,
}
"""Every name OpenSSH reads for a key type, and the type it names.

The signature names ``rsa-sha2-256`` and ``rsa-sha2-512`` (and their certificates') name an
``ssh-rsa`` key, and ``webauthn-sk-ecdsa-sha2-nistp256@openssh.com`` an ECDSA security key."""

_PLAIN_OF = {
    **{kind: kind for kind in _FIELDS},
    **{_certificate_type(kind): kind for kind in _FIELDS},
}
"""The plain key type of each type."""


def key_type(name: str) -> str | None:
    """Tell which key type a name stands for, as OpenSSH reads it.

    Parameters
    ----------
    name
        A known_hosts line's type word, or the name a key blob starts with.

    Returns
    -------
    str or None
        The type, such as ``ssh-rsa`` for ``rsa-sha2-256``; `None` for a name OpenSSH does not
        read, a short name such as ``ED25519`` included.
    """
    return _NAMES.get(name)


@dataclass(frozen=True)
class SshKey:
    """A key or certificate OpenSSH reads.

    Parameters
    ----------
    type
        Its type (`key_type`), such as ``ssh-ed25519`` or ``ssh-rsa-cert-v01@openssh.com``.
    plain
        The blob of its plain key (a certificate's certified key), as OpenSSH writes it;
        ``ssh-keygen -l`` prints its SHA-256.
    """

    type: str
    plain: bytes

    @property
    def is_certificate(self) -> bool:
        """Tell whether this is a certificate.

        Returns
        -------
        bool
            Whether its type ends in `CERT_SUFFIX`.
        """
        return self.type.endswith(CERT_SUFFIX)


def read_key(blob: bytes, *, check_signature: bool = True) -> SshKey | None:
    """Read a key blob as OpenSSH does.

    Parameters
    ----------
    blob
        The base64-decoded key of a known_hosts or public key line.
    check_signature
        Whether a certificate's signature must verify. Without the check, every other field is
        still read, and any plain key OpenSSH accepts may be the authority, a security key too.

    Returns
    -------
    SshKey or None
        The key; `None` for a blob OpenSSH refuses: an unknown type, a field too short or
        out of range, a byte left over, or a certificate that is malformed or whose
        signature does not verify.
    """
    try:
        reader = _Reader(blob)
        kind = _type(reader)
        plain = _PLAIN_OF[kind]
        if kind != plain:
            reader.string()
        key = _FIELDS[plain](reader)
        if kind != plain:
            _certificate(reader, blob, check_signature=check_signature)
        reader.end()
    except _Refused:
        return None
    return SshKey(type=kind, plain=_string(plain.encode()) + key.fields)


def _type(reader: _Reader) -> str:
    """Read the type a blob starts with.

    Parameters
    ----------
    reader
        The blob.

    Returns
    -------
    str
        The type it names (`key_type`).

    Raises
    ------
    _Refused
        For a name OpenSSH does not read.
    """
    kind = key_type(reader.cstring().decode("latin-1"))
    if kind is None:
        raise _Refused
    return kind


def _certificate(reader: _Reader, blob: bytes, *, check_signature: bool) -> None:
    """Read a certificate's fields after its key, and verify its signature.

    Parameters
    ----------
    reader
        The certificate, read up to the end of its key's fields.
    blob
        The whole certificate.
    check_signature
        Whether the signature must verify (`read_key`).

    Raises
    ------
    _Refused
        For a type other than user or host, a key id or principal with a NUL inside, more than
        `MAX_PRINCIPALS` principals, an option or extension without its value, an authority
        key that is no plain key Lakana checks, or a signature that does not verify.
    """
    reader.uint64()
    if reader.uint32() not in _CERT_TYPES:
        raise _Refused
    reader.cstring()
    principals = _Reader(reader.string())
    count = 0
    while not principals.done():
        if count == MAX_PRINCIPALS:
            raise _Refused
        principals.cstring()
        count += 1
    reader.uint64()
    reader.uint64()
    for _section in range(2):
        section = _Reader(reader.string())
        while not section.done():
            section.string()
            section.string()
    reader.string()
    authority = _Reader(reader.string())
    signed = blob[: reader.offset]
    signature = reader.string()
    kind = _type(authority)
    if kind not in _FIELDS or (check_signature and kind not in _SIGNERS):
        raise _Refused
    key = _FIELDS[kind](authority)
    authority.end()
    if check_signature:
        _verify(kind, key.public, signature, signed)


def _verify(kind: str, public: bytes | tuple[int, int], signature: bytes, data: bytes) -> None:
    """Verify a certificate authority's signature, as ``sshkey_verify``.

    Parameters
    ----------
    kind
        The authority key's type, one of `_SIGNERS`.
    public
        The authority key (`_Plain.public`).
    signature
        The signature blob: its name and its bytes.
    data
        What was signed.

    Raises
    ------
    _Refused
        When the signature is malformed, of another type, or does not verify.
    """
    reader = _Reader(signature)
    name = reader.cstring().decode("latin-1")
    value = reader.string()
    reader.end()
    try:
        if isinstance(public, tuple):
            _verify_rsa(name, public, value, data)
        elif name != kind:
            raise _Refused
        elif kind == "ssh-ed25519":
            ed25519.Ed25519PublicKey.from_public_bytes(public).verify(value, data)
        else:
            curve = _CURVES[kind]
            numbers = _Reader(value)
            r, s = numbers.mpint(), numbers.mpint()
            numbers.end()
            key = ec.EllipticCurvePublicKey.from_encoded_point(curve.curve, public)
            key.verify(encode_dss_signature(r, s), data, ec.ECDSA(curve.hash))
    except InvalidSignature:
        raise _Refused from None


def _verify_rsa(name: str, public: tuple[int, int], value: bytes, data: bytes) -> None:
    """Verify an RSA signature, as ``ssh_rsa_verify``.

    Parameters
    ----------
    name
        The signature's name, which picks the hash.
    public
        The key's ``(e, n)``.
    value
        The signature's bytes; shorter than the modulus is allowed.
    data
        What was signed.

    Raises
    ------
    _Refused
        For an unknown name, a signature longer than the modulus, or a key `cryptography`
        cannot verify with.
    """
    algorithm = _RSA_HASHES.get(name)
    e, n = public
    size = (n.bit_length() + 7) // 8
    if algorithm is None or len(value) > size:
        raise _Refused
    try:
        key = rsa.RSAPublicNumbers(e, n).public_key()
    except ValueError:
        raise _Refused from None
    key.verify(value.rjust(size, b"\0"), data, padding.PKCS1v15(), algorithm())
