"""Make a CA and a server certificate for `localhost` and `127.0.0.1`, for the example's API.

The REST recipes read an HTTPS API that the example starts on this machine (stand_in.py). Its
certificate is signed by a CA made here, which the connection is given with `--ca-file`, as a
team gives the CA of an internal API. Nothing is kept: every run makes new keys.

Usage: python3 make_certs.py DIRECTORY   (writes ca.pem, server.pem and server-key.pem)
"""

import datetime as dt
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

directory = Path(sys.argv[1])
directory.mkdir(parents=True, exist_ok=True)
now = dt.datetime.now(dt.UTC)
ca_key = ec.generate_private_key(ec.SECP256R1())
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example API CA")])
ca = (
    x509.CertificateBuilder()
    .subject_name(ca_name)
    .issuer_name(ca_name)
    .public_key(ca_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - dt.timedelta(minutes=5))
    .not_valid_after(now + dt.timedelta(days=1))
    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=False,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
    .sign(ca_key, hashes.SHA256())
)
key = ec.generate_private_key(ec.SECP256R1())
server = (
    x509.CertificateBuilder()
    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
    .issuer_name(ca_name)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - dt.timedelta(minutes=5))
    .not_valid_after(now + dt.timedelta(days=1))
    .add_extension(
        x509.SubjectAlternativeName(
            [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        ),
        critical=False,
    )
    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    .add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
    )
    .sign(ca_key, hashes.SHA256())
)
(directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
(directory / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
(directory / "server-key.pem").write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
(directory / "server-key.pem").chmod(0o600)
