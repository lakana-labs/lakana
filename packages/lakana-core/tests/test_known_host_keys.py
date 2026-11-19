"""The host keys a known_hosts text names, read as OpenSSH reads them (#732).

The keys and certificates in `host_keys/` were made by ssh-keygen (`host_keys/make.sh`), and
`host_keys/ssh-keygen-l.txt` holds what `ssh-keygen -l` printed for each. A broken key is one of
those with bytes cut, added or changed; a certificate with fields ssh-keygen never writes is
signed here by a key `cryptography` makes. Every case of `CASES` is also handed to ssh-keygen
when it is installed: the keys it lists for the host (`ssh-keygen -l -F`) must be the keys
`known_host_keys` lists.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess  # nosec B404 -- runs ssh-keygen on a file the test writes
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lakana_core.projects import probes
from lakana_core.projects.probes import (
    KEY_TYPES,
    MAX_HOST_KEYS,
    HostKeyMarker,
    KnownHostKey,
    key_fingerprint,
    known_host_keys,
    known_hosts_fingerprints,
    parse_keyscan,
)

FIXTURES = Path(__file__).parent / "host_keys"
HOST = "git.example.com"


def _pub(name: str) -> tuple[str, str]:
    """Read a fixture's type word and base64 key.

    Parameters
    ----------
    name
        The file in `host_keys/`.

    Returns
    -------
    tuple of str
        The type word and the base64 key.
    """
    word, encoded = (FIXTURES / name).read_text().split()[:2]
    return word, encoded


def _blob(name: str) -> bytes:
    """Decode a fixture's key.

    Parameters
    ----------
    name
        The file in `host_keys/`.

    Returns
    -------
    bytes
        The key blob.
    """
    return base64.b64decode(_pub(name)[1])


SSH_KEYGEN_L = {
    line.split()[0]: line.split()[2]
    for line in (FIXTURES / "ssh-keygen-l.txt").read_text().splitlines()
}
"""The fingerprint `ssh-keygen -l` printed for each fixture."""


def _listed(name: str, marker: HostKeyMarker | None = None) -> KnownHostKey:
    """Build the entry a fixture's own line gives: its type and ssh-keygen's fingerprint.

    Parameters
    ----------
    name
        The file in `host_keys/`.
    marker
        The line's marker without its ``@``.

    Returns
    -------
    KnownHostKey
        The expected entry.
    """
    return KnownHostKey(type=_pub(name)[0], fingerprint=SSH_KEYGEN_L[name], marker=marker)


def _enc(blob: bytes) -> str:
    """Encode a blob as a known_hosts line carries it.

    Parameters
    ----------
    blob
        The bytes.

    Returns
    -------
    str
        Padded base64.
    """
    return base64.b64encode(blob).decode()


def _string(data: bytes) -> bytes:
    """Write an SSH string: a four-byte length and the bytes.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    bytes
        The string.
    """
    return len(data).to_bytes(4, "big") + data


def _mpint(value: int) -> bytes:
    """Write an SSH mpint, as short as it can be.

    Parameters
    ----------
    value
        A number of zero or more.

    Returns
    -------
    bytes
        The mpint as an SSH string.
    """
    data = value.to_bytes((value.bit_length() + 8) // 8, "big") if value else b""
    return _string(data)


def _strings(blob: bytes) -> list[bytes]:
    """Split a plain key blob into its SSH strings.

    Parameters
    ----------
    blob
        A plain key blob, whose every field is a string or an mpint.

    Returns
    -------
    list of bytes
        Each field's bytes.
    """
    fields: list[bytes] = []
    while blob:
        size = int.from_bytes(blob[:4], "big")
        fields.append(blob[4 : 4 + size])
        blob = blob[4 + size :]
    return fields


def _line(word: str, encoded: str, marker: str = "") -> str:
    """Write one known_hosts line for `HOST`.

    Parameters
    ----------
    word
        The type word.
    encoded
        The base64 key.
    marker
        ``@cert-authority ``, ``@revoked `` or nothing.

    Returns
    -------
    str
        The line and its newline.
    """
    return f"{marker}{HOST} {word} {encoded}\n"


def _own(name: str, marker: str = "") -> str:
    """Write a fixture's key as a line, with the word its file names.

    Parameters
    ----------
    name
        The file in `host_keys/`.
    marker
        ``@cert-authority ``, ``@revoked `` or nothing.

    Returns
    -------
    str
        The line.
    """
    return _line(*_pub(name), marker=marker)


# --- certificates signed here ---------------------------------------------------------------

CertificateAuthority = ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey

ED25519_CA = ed25519.Ed25519PrivateKey.generate()
ECDSA_CA = ec.generate_private_key(ec.SECP256R1())
RSA_CA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_blob(ca: CertificateAuthority) -> bytes:
    """Write the SSH key blob of a certificate authority's public key.

    Parameters
    ----------
    ca
        The private key.

    Returns
    -------
    bytes
        The public key blob.
    """
    line = ca.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    return base64.b64decode(line.split()[1])


def _signature(ca: CertificateAuthority, data: bytes) -> bytes:
    """Sign as ssh-keygen signs a certificate: the signature blob.

    Parameters
    ----------
    ca
        The certificate authority's private key.
    data
        The certificate up to its signature.

    Returns
    -------
    bytes
        ``string type, string signature``, RSA with ``rsa-sha2-512``.
    """
    if isinstance(ca, ed25519.Ed25519PrivateKey):
        return _string(b"ssh-ed25519") + _string(ca.sign(data))
    if isinstance(ca, ec.EllipticCurvePrivateKey):
        r, s = decode_dss_signature(ca.sign(data, ec.ECDSA(hashes.SHA256())))
        return _string(b"ecdsa-sha2-nistp256") + _string(_mpint(r) + _mpint(s))
    return _string(b"rsa-sha2-512") + _string(ca.sign(data, padding.PKCS1v15(), hashes.SHA512()))


ED25519_PUBLIC = _strings(_blob("host_ed25519.pub"))[1]
"""The ed25519 host key's 32 bytes, which the certificates below certify."""


def _certificate(
    *,
    kind: int = 2,
    key_id: bytes = b"host",
    principals: list[bytes] | None = None,
    critical: bytes = b"",
    extensions: bytes = b"",
    ca: CertificateAuthority = ED25519_CA,
    ca_blob: bytes | None = None,
    sign: Callable[[bytes], bytes] | None = None,
    key: bytes | None = None,
    name: bytes = b"ssh-ed25519-cert-v01@openssh.com",
) -> str:
    """Sign a host certificate of the ed25519 host key, in base64.

    Parameters
    ----------
    kind
        The certificate type: 1 for a user, 2 for a host.
    key_id
        The key id.
    principals
        Each principal; `HOST` alone when `None`.
    critical
        The critical options section.
    extensions
        The extensions section.
    ca
        The certificate authority that signs.
    ca_blob
        The signature key the certificate names; `ca`'s own when `None`.
    sign
        Builds the signature blob from the signed bytes; `_signature` with `ca` when `None`.
    key
        The key fields after the nonce; the ed25519 host key's when `None`.
    name
        The certificate's type string.

    Returns
    -------
    str
        The certificate's base64.
    """
    listed = [HOST.encode()] if principals is None else principals
    body = (
        _string(name)
        + _string(bytes(32))
        + (_string(ED25519_PUBLIC) if key is None else key)
        + (1).to_bytes(8, "big")
        + kind.to_bytes(4, "big")
        + _string(key_id)
        + _string(b"".join(_string(p) for p in listed))
        + bytes(8)
        + b"\xff" * 8
        + _string(critical)
        + _string(extensions)
        + _string(b"")
        + _string(_public_blob(ca) if ca_blob is None else ca_blob)
    )
    signature = _signature(ca, body) if sign is None else sign(body)
    return _enc(body + _string(signature))


ED25519_CERT_WORD = "ssh-ed25519-cert-v01@openssh.com"
CERTIFIED = KnownHostKey(
    type=ED25519_CERT_WORD, fingerprint=SSH_KEYGEN_L["host_ed25519.pub"], marker="cert-authority"
)
"""What a well-formed certificate of the ed25519 host key lists on a ``@cert-authority`` line.

ssh never trusts a host through a certificate on a line without a marker, so such a line is not
listed; under ``@cert-authority`` the certified key signs host certificates, as ssh reads it."""


def _authority(word: str, encoded: str) -> str:
    """Write a ``@cert-authority`` line for `HOST`.

    Parameters
    ----------
    word
        The type word.
    encoded
        The base64 key.

    Returns
    -------
    str
        The line and its newline.
    """
    return _line(word, encoded, "@cert-authority ")


# --- keys changed by hand -------------------------------------------------------------------

RSA_NAME, RSA_E, RSA_N = _strings(_blob("host_rsa.pub"))
ECDSA_NAME, ECDSA_CURVE, ECDSA_POINT = _strings(_blob("host_ecdsa256.pub"))

P256 = (
    2**256 - 2**224 + 2**192 + 2**96 - 1,
    0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
)
"""NIST P-256's prime, its ``b`` (``a`` is -3) and the order of its group."""


def _p256_point(x: int) -> bytes:
    """Find the first P-256 point whose x is `x` or more, uncompressed.

    Parameters
    ----------
    x
        Where to start looking.

    Returns
    -------
    bytes
        ``04``, x and y, 32 bytes each.
    """
    p, b, _order = P256
    while True:
        y_squared = (x**3 - 3 * x + b) % p
        y = pow(y_squared, (p + 1) // 4, p)
        if y * y % p == y_squared:
            return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
        x += 1


def _ecdsa(point: bytes, curve: bytes = ECDSA_CURVE) -> str:
    """Write an ecdsa-sha2-nistp256 key blob with another point or curve name, in base64.

    Parameters
    ----------
    point
        The point's bytes.
    curve
        The curve name the blob names.

    Returns
    -------
    str
        The base64 key.
    """
    return _enc(_string(ECDSA_NAME) + _string(curve) + _string(point))


def _rsa(e: bytes = RSA_E, n: bytes = RSA_N, name: bytes = RSA_NAME) -> str:
    """Write an RSA key blob with other fields, in base64.

    Parameters
    ----------
    e
        The exponent's mpint bytes.
    n
        The modulus's mpint bytes.
    name
        The type string the blob starts with.

    Returns
    -------
    str
        The base64 key.
    """
    return _enc(_string(name) + _string(e) + _string(n))


def _compressed(point: bytes) -> bytes:
    """Write a P-256 point compressed.

    Parameters
    ----------
    point
        The uncompressed point.

    Returns
    -------
    bytes
        ``02`` or ``03`` and x.
    """
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    return key.public_bytes(Encoding.X962, PublicFormat.CompressedPoint)


def _tampered(name: str) -> str:
    """Change the last byte of a fixture certificate's signature.

    Parameters
    ----------
    name
        A certificate in `host_keys/`.

    Returns
    -------
    str
        The base64 key.
    """
    blob = _blob(name)
    return _enc(blob[:-1] + bytes([blob[-1] ^ 1]))


ED25519_WORD, ED25519_B64 = _pub("host_ed25519.pub")
ED25519_BLOB = _blob("host_ed25519.pub")
RSA_CERT = _pub("host_rsa-cert.pub")[1]
SK_ED25519 = "sk-ssh-ed25519@openssh.com"
SK_ECDSA = "sk-ecdsa-sha2-nistp256@openssh.com"
SK_ED25519_BLOB = _string(SK_ED25519.encode()) + _string(ED25519_PUBLIC) + _string(b"ssh:")
SK_ECDSA_BLOB = (
    _string(SK_ECDSA.encode()) + _string(ECDSA_CURVE) + _string(ECDSA_POINT) + _string(b"ssh:")
)
BIG_N = (RSA_N[1:] * 6)[:2048]
"""A 16,384-bit modulus, the largest OpenSSH reads."""


def _ed25519_signed_by_rsa(sig_type: bytes, signature: Callable[[bytes], bytes]) -> str:
    """Sign a certificate by `RSA_CA` with a signature `signature` builds.

    Parameters
    ----------
    sig_type
        The signature's type string.
    signature
        Builds the signature bytes from the signed bytes.

    Returns
    -------
    str
        The certificate's base64.
    """
    return _certificate(ca=RSA_CA, sign=lambda body: _string(sig_type) + _string(signature(body)))


def _rsa_sign(body: bytes) -> bytes:
    """Sign with `RSA_CA`, SHA-512.

    Parameters
    ----------
    body
        The signed bytes.

    Returns
    -------
    bytes
        The signature.
    """
    return RSA_CA.sign(body, padding.PKCS1v15(), hashes.SHA512())


def _ecdsa_sig(body: bytes, extra: bytes = b"", sig_type: bytes = b"ecdsa-sha2-nistp256") -> bytes:
    """Sign with `ECDSA_CA`, adding bytes after r and s.

    Parameters
    ----------
    body
        The signed bytes.
    extra
        Bytes after the two numbers.
    sig_type
        The signature's type string.

    Returns
    -------
    bytes
        The signature blob.
    """
    r, s = decode_dss_signature(ECDSA_CA.sign(body, ec.ECDSA(hashes.SHA256())))
    return _string(sig_type) + _string(_mpint(r) + _mpint(s) + extra)


def _rsa_ca_blob(e: int) -> bytes:
    """`RSA_CA`'s public key with another exponent.

    Parameters
    ----------
    e
        The exponent.

    Returns
    -------
    bytes
        The key blob.
    """
    numbers = RSA_CA.public_key().public_numbers()
    return _string(b"ssh-rsa") + _mpint(e) + _mpint(numbers.n)


RSA_KEY = _listed("host_rsa.pub")


def _security_key_signature(body: bytes) -> bytes:
    """Sign as an ed25519 security key does, with `ED25519_CA` as the authenticator's key.

    Parameters
    ----------
    body
        The certificate up to its signature.

    Returns
    -------
    bytes
        ``string type, string signature, byte flags, uint32 counter`` over
        ``sha256(application) || flags || counter || sha256(body)``.
    """
    flags, counter = b"\x01", (1).to_bytes(4, "big")
    signed = hashlib.sha256(b"ssh:").digest() + flags + counter + hashlib.sha256(body).digest()
    return _string(SK_ED25519.encode()) + _string(ED25519_CA.sign(signed)) + flags + counter


SK_CA_BLOB = (
    _string(SK_ED25519.encode()) + _string(_public_blob(ED25519_CA)[19:]) + _string(b"ssh:")
)
"""`ED25519_CA` as an ed25519 security key."""

SHA512_DIGEST_INFO = bytes.fromhex("3051300d060960864801650304020305000440")


def _exponent_one_signature(body: bytes) -> bytes:
    """Sign for an RSA key whose exponent is 1: the PKCS #1 v1.5 block itself.

    OpenSSH verifies it (``s ** 1 mod n`` is the block); `cryptography` refuses such a key.

    Parameters
    ----------
    body
        The certificate up to its signature.

    Returns
    -------
    bytes
        An ``rsa-sha2-512`` signature blob for `_rsa_ca_blob` (1).
    """
    size = (RSA_CA.public_key().key_size + 7) // 8
    digest = SHA512_DIGEST_INFO + hashlib.sha512(body).digest()
    block = b"\x00\x01" + b"\xff" * (size - 3 - len(digest)) + b"\x00" + digest
    return _string(b"rsa-sha2-512") + _string(block)


def _revoked(encoded: str) -> str:
    """Write a ``@revoked`` certificate line of the ed25519 host key, beside its plain line.

    Parameters
    ----------
    encoded
        The certificate's base64.

    Returns
    -------
    str
        The two lines.
    """
    return _line(ED25519_CERT_WORD, encoded, "@revoked ") + _own("host_ed25519.pub")


REVOKED_CERTIFICATE = [
    _listed("host_ed25519.pub"),
    KnownHostKey(
        type=ED25519_CERT_WORD, fingerprint=SSH_KEYGEN_L["host_ed25519.pub"], marker="revoked"
    ),
]
"""A revoked certificate of the ed25519 host key, listed beside the key itself."""
RSA_B64 = _pub("host_rsa.pub")[1]


def _unlisted(key_type: str, encoded: str) -> KnownHostKey:
    """Build the entry of a key no fixture holds, hashed as it is written.

    Parameters
    ----------
    key_type
        Its type.
    encoded
        Its base64, written as short as it can be.

    Returns
    -------
    KnownHostKey
        The expected entry.
    """
    return KnownHostKey(type=key_type, fingerprint=key_fingerprint(base64.b64decode(encoded)))


BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _with_slop(encoded: str) -> str:
    """Set a bit after the last byte of padded base64, which a decoder could ignore.

    Parameters
    ----------
    encoded
        Base64 ending in ``=``.

    Returns
    -------
    str
        The same bytes with the last character's unused low bit set.
    """
    core = encoded.rstrip("=")
    last = BASE64[BASE64.index(core[-1]) | 1]
    return core[:-1] + last + encoded[len(core) :]


CASES: list[tuple[str, str, list[KnownHostKey]]] = [
    # The cases #732 names.
    ("trailing bytes", _line(ED25519_WORD, ED25519_B64 + "AAAA"), []),
    ("truncated", _line(ED25519_WORD, _enc(ED25519_BLOB[:-1])), []),
    (
        "rsa-sha2 certificate words",
        _authority("rsa-sha2-256-cert-v01@openssh.com", RSA_CERT)
        + _authority("rsa-sha2-512-cert-v01@openssh.com", RSA_CERT),
        [_listed("host_rsa-cert.pub", "cert-authority")],
    ),
    (
        "a revoked certificate",
        _line("rsa-sha2-256-cert-v01@openssh.com", RSA_CERT, "@revoked "),
        [_listed("host_rsa-cert.pub", "revoked")],
    ),
    (
        "one RSA key under three words",
        _line("ssh-rsa", RSA_B64) + _line("rsa-sha2-256", RSA_B64) + _line("rsa-sha2-512", RSA_B64),
        [RSA_KEY],
    ),
    (
        "a certificate authority",
        _own("ca_ed25519.pub", "@cert-authority "),
        [_listed("ca_ed25519.pub", "cert-authority")],
    ),
    # Every fixture as its own line.
    *(
        (name, _own(name), [_listed(name)])
        for name in sorted(SSH_KEYGEN_L)
        if not name.startswith("ca_") and not name.endswith("-cert.pub")
    ),
    *(
        (name, _own(name, "@cert-authority "), [_listed(name, "cert-authority")])
        for name in sorted(SSH_KEYGEN_L)
        if name.endswith("-cert.pub")
    ),
    ("a certificate without a marker", _own("host_rsa-cert.pub"), []),
    (
        "a certificate without a marker beside its key",
        _own("host_ed25519-cert.pub") + _own("host_ed25519.pub"),
        [_listed("host_ed25519.pub")],
    ),
    (
        "one key plain and revoked",
        _own("host_ed25519.pub") + _own("host_ed25519.pub", "@revoked "),
        [_listed("host_ed25519.pub"), _listed("host_ed25519.pub", "revoked")],
    ),
    # Lines.
    ("a comment", f"# {HOST} {ED25519_WORD} {ED25519_B64}\n", []),
    (
        "tabs and leading blanks",
        f" \t{HOST}\t{ED25519_WORD}\t{ED25519_B64}\t# c\n",
        [_listed("host_ed25519.pub")],
    ),
    (
        "a carriage return",
        f"{HOST} {ED25519_WORD} {ED25519_B64}\r\n",
        [_listed("host_ed25519.pub")],
    ),
    ("an unknown marker", _own("host_ed25519.pub", "@trusted "), []),
    ("two markers", _own("host_ed25519.pub", "@revoked @revoked "), []),
    ("a marker alone", "@revoked\n", []),
    ("no key", f"{HOST} {ED25519_WORD}\n", []),
    (
        "a host that starts like a comment",
        f"@revoked #x,{HOST} {ED25519_WORD} {ED25519_B64}\n",
        [_listed("host_ed25519.pub", "revoked")],
    ),
    # Base64.
    ("a non-ASCII character in the key", _line(ED25519_WORD, "AAAA\u00e9"), []),
    (
        "a no-break space before a comment",
        f"{HOST} {ED25519_WORD} {ED25519_B64}\u00a0# pasted\n",
        [],
    ),
    ("not base64", _line(ED25519_WORD, "AAAA*AAA"), []),
    ("missing padding", _line("ecdsa-sha2-nistp384", _pub("host_ecdsa384.pub")[1].rstrip("=")), []),
    (
        "bits after the last byte",
        _line(ECDSA_NAME.decode(), _with_slop(_pub("host_ecdsa256.pub")[1])),
        [],
    ),
    (
        "a vertical tab inside",
        _line(ED25519_WORD, ED25519_B64[:8] + "\v" + ED25519_B64[8:]),
        [_listed("host_ed25519.pub")],
    ),
    # Type words.
    ("a word the key contradicts", _line("ssh-rsa", ED25519_B64), []),
    ("another curve's word", _line("ecdsa-sha2-nistp384", _pub("host_ecdsa256.pub")[1]), []),
    ("a short name", _line("ED25519", ED25519_B64), []),
    ("an unknown word", _line("ssh-dss", ED25519_B64), []),
    ("a certificate's word for a plain key", _line(ED25519_CERT_WORD, ED25519_B64), []),
    ("a plain word for a certificate", _line(ED25519_WORD, _pub("host_ed25519-cert.pub")[1]), []),
    ("a key that names no type", _line(ED25519_WORD, _enc(_string(b""))), []),
    ("an empty blob", _line(ED25519_WORD, ""), []),
    # ed25519.
    (
        "an ed25519 key of 33 bytes",
        _line(ED25519_WORD, _enc(_string(b"ssh-ed25519") + _string(ED25519_PUBLIC + b"\0"))),
        [],
    ),
    # ECDSA.
    ("a compressed point", _line(ECDSA_NAME.decode(), _ecdsa(_compressed(ECDSA_POINT))), []),
    (
        "a point off the curve",
        _line(ECDSA_NAME.decode(), _ecdsa(ECDSA_POINT[:-1] + bytes([ECDSA_POINT[-1] ^ 1]))),
        [],
    ),
    ("another curve's name", _line(ECDSA_NAME.decode(), _ecdsa(ECDSA_POINT, b"nistp384")), []),
    ("a small x", _line(ECDSA_NAME.decode(), _ecdsa(_p256_point(5))), []),
    ("an x past the order", _line(ECDSA_NAME.decode(), _ecdsa(_p256_point(P256[2] - 1))), []),
    (
        "an x just large enough",
        _line(ECDSA_NAME.decode(), _ecdsa(_p256_point(2**128))),
        [_unlisted("ecdsa-sha2-nistp256", _ecdsa(_p256_point(2**128)))],
    ),
    # RSA.
    ("an exponent with a leading zero", _line("ssh-rsa", _rsa(e=b"\0" + RSA_E)), [RSA_KEY]),
    ("a blob named rsa-sha2-256", _line("ssh-rsa", _rsa(name=b"rsa-sha2-256")), [RSA_KEY]),
    ("a negative modulus", _line("ssh-rsa", _rsa(n=RSA_N[1:])), []),
    ("a 1016-bit modulus", _line("ssh-rsa", _rsa(n=RSA_N[:128])), []),
    (
        "a 1024-bit modulus",
        _line("ssh-rsa", _rsa(n=RSA_N[:129])),
        [_unlisted("ssh-rsa", _rsa(n=RSA_N[:129]))],
    ),
    (
        "an exponent of zero",
        _line("ssh-rsa", _rsa(e=b"")),
        [_unlisted("ssh-rsa", _rsa(e=b""))],
    ),
    (
        "a 16384-bit modulus",
        _line("ssh-rsa", _rsa(n=b"\0" + BIG_N)),
        [_unlisted("ssh-rsa", _rsa(n=b"\0" + BIG_N))],
    ),
    ("a modulus past 16384 bits", _line("ssh-rsa", _rsa(n=b"\x01" + BIG_N)), []),
    # Security keys.
    (
        "a security key as authority",
        _line(SK_ED25519, _enc(SK_ED25519_BLOB), "@cert-authority "),
        [
            KnownHostKey(
                type=SK_ED25519,
                fingerprint=key_fingerprint(SK_ED25519_BLOB),
                marker="cert-authority",
            )
        ],
    ),
    (
        "an ecdsa security key",
        _line(SK_ECDSA, _enc(SK_ECDSA_BLOB)),
        [KnownHostKey(type=SK_ECDSA, fingerprint=key_fingerprint(SK_ECDSA_BLOB))],
    ),
    (
        "an ecdsa security key under its webauthn word",
        _line("webauthn-sk-ecdsa-sha2-nistp256@openssh.com", _enc(SK_ECDSA_BLOB)),
        [KnownHostKey(type=SK_ECDSA, fingerprint=key_fingerprint(SK_ECDSA_BLOB))],
    ),
    (
        "a security key's application with a NUL",
        _line(SK_ED25519, _enc(SK_ED25519_BLOB[:-8] + _string(b"ss\0:"))),
        [],
    ),
    (
        "a security key's application ending in a NUL",
        _line(SK_ED25519, _enc(SK_ED25519_BLOB[:-8] + _string(b"ssh:\0"))),
        [KnownHostKey(type=SK_ED25519, fingerprint=key_fingerprint(SK_ED25519_BLOB))],
    ),
    (
        "a type name ending in a NUL",
        _line(ED25519_WORD, _enc(_string(b"ssh-ed25519\0") + _string(ED25519_PUBLIC))),
        [_listed("host_ed25519.pub")],
    ),
    (
        "a security key's certificate",
        _authority(
            "sk-ssh-ed25519-cert-v01@openssh.com",
            _certificate(
                name=b"sk-ssh-ed25519-cert-v01@openssh.com",
                key=_string(ED25519_PUBLIC) + _string(b"ssh:"),
            ),
        ),
        [
            KnownHostKey(
                type="sk-ssh-ed25519-cert-v01@openssh.com",
                fingerprint=key_fingerprint(SK_ED25519_BLOB),
                marker="cert-authority",
            )
        ],
    ),
    # Certificates of ssh-keygen with a byte changed.
    *(
        (f"{name} with another signature", _authority(_pub(name)[0], _tampered(name)), [])
        for name in sorted(SSH_KEYGEN_L)
        if name.endswith("-cert.pub")
    ),
    (
        "a certificate with a byte after it",
        _authority(ED25519_CERT_WORD, _enc(_blob("host_ed25519-cert.pub") + b"\0")),
        [],
    ),
    # Certificates signed here.
    ("a certificate signed here", _authority(ED25519_CERT_WORD, _certificate()), [CERTIFIED]),
    ("a user certificate", _authority(ED25519_CERT_WORD, _certificate(kind=1)), [CERTIFIED]),
    ("a certificate of type 3", _authority(ED25519_CERT_WORD, _certificate(kind=3)), []),
    ("a key id with a NUL", _authority(ED25519_CERT_WORD, _certificate(key_id=b"a\0b")), []),
    (
        "a principal with a NUL",
        _authority(ED25519_CERT_WORD, _certificate(principals=[b"a\0b"])),
        [],
    ),
    (
        "256 principals",
        _authority(ED25519_CERT_WORD, _certificate(principals=[b"p"] * 256)),
        [CERTIFIED],
    ),
    ("257 principals", _authority(ED25519_CERT_WORD, _certificate(principals=[b"p"] * 257)), []),
    (
        "options and extensions",
        _authority(
            ED25519_CERT_WORD,
            _certificate(
                critical=_string(b"force-command") + _string(_string(b"true")),
                extensions=_string(b"permit-pty") + _string(b""),
            ),
        ),
        [CERTIFIED],
    ),
    (
        "an option without its value",
        _authority(ED25519_CERT_WORD, _certificate(critical=_string(b"x"))),
        [],
    ),
    (
        "an extension without its value",
        _authority(ED25519_CERT_WORD, _certificate(extensions=_string(b"x"))),
        [],
    ),
    (
        "signed by an ECDSA key",
        _authority(ED25519_CERT_WORD, _certificate(ca=ECDSA_CA)),
        [CERTIFIED],
    ),
    ("signed by an RSA key", _authority(ED25519_CERT_WORD, _certificate(ca=RSA_CA)), [CERTIFIED]),
    (
        "signed by a certificate",
        _authority(ED25519_CERT_WORD, _certificate(ca_blob=_blob("host_ed25519-cert.pub"))),
        [],
    ),
    (
        "signed by a security key",
        _authority(ED25519_CERT_WORD, _certificate(ca_blob=SK_ED25519_BLOB)),
        [],
    ),
    (
        "an ed25519 signature named otherwise",
        _authority(
            ED25519_CERT_WORD,
            _certificate(sign=lambda body: _string(b"ssh-rsa") + _string(ED25519_CA.sign(body))),
        ),
        [],
    ),
    (
        "a signature blob with a byte after it",
        _authority(
            ED25519_CERT_WORD, _certificate(sign=lambda body: _signature(ED25519_CA, body) + b"\0")
        ),
        [],
    ),
    (
        "an ECDSA signature named otherwise",
        _authority(
            ED25519_CERT_WORD,
            _certificate(
                ca=ECDSA_CA, sign=lambda body: _ecdsa_sig(body, sig_type=b"ecdsa-sha2-nistp384")
            ),
        ),
        [],
    ),
    (
        "an ECDSA signature with a byte after s",
        _authority(
            ED25519_CERT_WORD, _certificate(ca=ECDSA_CA, sign=lambda body: _ecdsa_sig(body, b"\0"))
        ),
        [],
    ),
    (
        "an RSA signature named ssh-rsa over SHA-512",
        _authority(ED25519_CERT_WORD, _ed25519_signed_by_rsa(b"ssh-rsa", _rsa_sign)),
        [],
    ),
    (
        "an RSA signature of an unknown name",
        _authority(ED25519_CERT_WORD, _ed25519_signed_by_rsa(b"rsa-sha2-384", _rsa_sign)),
        [],
    ),
    (
        "an RSA signature longer than the modulus",
        _authority(
            ED25519_CERT_WORD,
            _ed25519_signed_by_rsa(b"rsa-sha2-512", lambda body: b"\0" + _rsa_sign(body)),
        ),
        [],
    ),
    (
        "an RSA signature without its leading zero",
        _authority(
            ED25519_CERT_WORD,
            _ed25519_signed_by_rsa(b"rsa-sha2-512", lambda body: _rsa_sign(body).lstrip(b"\0")),
        ),
        [CERTIFIED],
    ),
    (
        "an RSA authority whose exponent is even",
        _authority(ED25519_CERT_WORD, _certificate(ca=RSA_CA, ca_blob=_rsa_ca_blob(65538))),
        [],
    ),
    # A revoked certificate whose signature Lakana does not check.
    (
        "a revoked certificate signed by a security key",
        _revoked(_certificate(ca_blob=SK_CA_BLOB, sign=_security_key_signature)),
        REVOKED_CERTIFICATE,
    ),
    (
        "a revoked certificate signed by an RSA key whose exponent is 1",
        _revoked(_certificate(ca=RSA_CA, ca_blob=_rsa_ca_blob(1), sign=_exponent_one_signature)),
        REVOKED_CERTIFICATE,
    ),
]

STRICTER: list[tuple[str, str, list[KnownHostKey]]] = [
    (
        "a revoked certificate with another signature",
        _revoked(_tampered("host_ed25519-cert.pub")),
        [
            _listed("host_ed25519.pub"),
            _listed("host_ed25519-cert.pub", "revoked"),
        ],
    ),
    (
        "a security key's signature on a cert-authority line",
        _authority(
            ED25519_CERT_WORD, _certificate(ca_blob=SK_CA_BLOB, sign=_security_key_signature)
        ),
        [],
    ),
]
"""Where the list differs from ssh-keygen; it never shows as trusted a key ssh refuses.

A certificate on a ``@revoked`` line is listed without its signature checked, since revoking a
key can only make ssh refuse more. One on another line is listed only when its signature
verifies, which Lakana cannot check for a security key: such an authority is left out."""


@pytest.mark.parametrize(
    ("text", "expected"), [pytest.param(t, e, id=i) for i, t, e in CASES + STRICTER]
)
def test_known_host_keys_lists_what_openssh_reads(text: str, expected: list[KnownHostKey]) -> None:
    """Each case lists the keys OpenSSH reads from its text, each once, by type and fingerprint.

    Parameters
    ----------
    text
        The known_hosts text.
    expected
        The keys it names.
    """
    assert known_host_keys(text) == expected


OPENSSH_TYPES = {
    "ssh-ed25519": "ED25519",
    "ecdsa-sha2-nistp256": "ECDSA",
    "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
    "ssh-rsa": "RSA",
    SK_ED25519: "ED25519-SK",
    SK_ECDSA: "ECDSA-SK",
}
"""The name `ssh-keygen -l` prints for each plain type; a certificate's adds ``-CERT``."""


def _short(key_type: str) -> str:
    """Name a type as `ssh-keygen -l` prints it.

    Parameters
    ----------
    key_type
        A type `known_host_keys` lists.

    Returns
    -------
    str
        Such as ``ED25519`` or ``RSA-CERT``.
    """
    plain = key_type.replace("-cert-v01@openssh.com", "")
    plain = plain if plain in OPENSSH_TYPES else plain + "@openssh.com"
    return OPENSSH_TYPES[plain] + ("-CERT" if plain != key_type else "")


def _openssh(text: str, path: Path) -> set[tuple[str, str, str | None]]:
    """List the keys ssh-keygen finds for `HOST` in a known_hosts text.

    Parameters
    ----------
    text
        The known_hosts text.
    path
        Where to write it.

    Returns
    -------
    set of tuple
        ssh-keygen's type name, fingerprint and marker of each key found, but a certificate on
        a line without a marker, which ssh never trusts a host through.
    """
    path.write_text(text)
    ssh_keygen = shutil.which("ssh-keygen")
    assert ssh_keygen is not None
    output = subprocess.run(  # nosec B603 -- a fixed program on a file the test wrote
        [ssh_keygen, "-l", "-F", HOST, "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    markers = {"CA": "cert-authority", "REVOKED": "revoked"}
    found: set[tuple[str, str, str | None]] = set()
    marker: str | None = None
    for line in output:
        if line.startswith("# Host"):
            marker = markers.get(line.split()[-1])
        else:
            _host, short, fingerprint = line.split()[:3]
            if marker is not None or not short.endswith("-CERT"):
                found.add((short, fingerprint, marker))
    return found


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed")
@pytest.mark.parametrize("text", [pytest.param(t, id=i) for i, t, _ in CASES])
def test_ssh_keygen_finds_the_same_keys(text: str, tmp_path: Path) -> None:
    """OpenSSH itself lists the keys `known_host_keys` lists, by type, fingerprint and marker.

    Parameters
    ----------
    text
        The known_hosts text.
    tmp_path
        Where the text is written for ssh-keygen.
    """
    ours = {(_short(k.type), k.fingerprint, k.marker) for k in known_host_keys(text)}
    assert ours == _openssh(text, tmp_path / "known_hosts")


def test_the_audit_counts_every_base64_key() -> None:
    """The audit's fingerprints still cover a key OpenSSH skips, hashed as it is written.

    Only a key that is not base64 at all is left out.
    """
    text = (
        _line(ED25519_WORD, ED25519_B64 + "AAAA")
        + _own("host_rsa-cert.pub")
        + _line(ED25519_WORD, "not*base64")
        + _line(ED25519_WORD, "AAAA\u00e9")
    )

    assert known_hosts_fingerprints(text) == sorted(
        {key_fingerprint(ED25519_BLOB + bytes(3)), key_fingerprint(_blob("host_rsa-cert.pub"))}
    )


def test_parse_keyscan_drops_a_malformed_key() -> None:
    """A key with a byte after it is not offered, as ssh would not read it."""
    ecdsa = _pub("host_ecdsa256.pub")[1]
    output = f"x {ED25519_WORD} {ED25519_B64}AAAA\nx ecdsa-sha2-nistp256 {ecdsa}\n"

    keys = parse_keyscan(output, host=HOST, port=22)

    assert [(k.type, k.fingerprint) for k in keys] == [
        ("ecdsa-sha2-nistp256", SSH_KEYGEN_L["host_ecdsa256.pub"])
    ]


def test_parse_keyscan_drops_a_key_with_a_non_ascii_character() -> None:
    """A character base64 has no place for drops the line rather than failing the scan."""
    output = f"x {ED25519_WORD} {ED25519_B64}\u00e9\nx {ED25519_WORD} {ED25519_B64}\n"

    keys = parse_keyscan(output, host=HOST, port=22)

    assert [k.fingerprint for k in keys] == [SSH_KEYGEN_L["host_ed25519.pub"]]


def test_one_line_is_read_once() -> None:
    """A line written again is not read again: a certificate's signature is checked once."""
    calls: list[bytes] = []
    real = probes.read_key

    def counting(blob: bytes, *, check_signature: bool) -> object:
        calls.append(blob)
        return real(blob, check_signature=check_signature)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(probes, "read_key", counting)
        keys = known_host_keys(_own("host_rsa-cert.pub", "@cert-authority ") * 50)

    assert keys == [_listed("host_rsa-cert.pub", "cert-authority")]
    assert len(calls) == 1


def test_parse_keyscan_keeps_at_most_ten_keys_and_no_duplicates() -> None:
    """A host that answers with many keys yields at most `MAX_HOST_KEYS`, each once."""
    blobs = [
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
        .split()[1]
        .decode()
        for _ in range(15)
    ]
    output = "".join(f"x ssh-ed25519 {b}\nx ssh-ed25519 {b}\n" for b in blobs)

    keys = parse_keyscan(output, host="h", port=22)

    assert len(keys) == MAX_HOST_KEYS == 10
    assert len({k.line for k in keys}) == 10
    assert KEY_TYPES[0] == "ssh-ed25519"
