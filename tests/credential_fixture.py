"""Generate installation-specific Skyport credentials; never ships a shared key."""

import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOT_BEFORE = datetime(1970, 1, 1, tzinfo=timezone.utc)
NOT_AFTER = datetime(2037, 12, 12, 23, 59, 59, tzinfo=timezone.utc)
HOSTNAMES = ("connect.skyport.io", "connectcl.skyport.io", "connectjs.skyport.io",
             "ctupdate.skyport.io", "*.skyport.io")
FILES = ("root-ca.pem", "root-ca.key", "server.pem", "server.key", "server-chain.pem")


def _public(key):
    return key.public_bytes(serialization.Encoding.DER,
                            serialization.PublicFormat.SubjectPublicKeyInfo)


def _write(path, contents):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)


def generate(directory):
    """Create a new directory and new random RSA keys. Existing paths are refused."""
    directory = Path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=False)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Skyport Root CA")])
    ca = (x509.CertificateBuilder()
          .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(NOT_BEFORE).not_valid_after(NOT_AFTER)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True,
                                      False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAMES[0])]))
            .issuer_name(ca_name).public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOT_BEFORE).not_valid_after(NOT_AFTER)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False,
                                        False, False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTNAMES]), False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_pem, leaf_pem = ca.public_bytes(pem), leaf.public_bytes(pem)
    key_format = serialization.PrivateFormat.TraditionalOpenSSL
    for filename, contents in {
        "root-ca.pem": ca_pem,
        "root-ca.key": ca_key.private_bytes(pem, key_format, serialization.NoEncryption()),
        "server.pem": leaf_pem,
        "server.key": server_key.private_bytes(pem, key_format, serialization.NoEncryption()),
        "server-chain.pem": leaf_pem + ca_pem,
    }.items():
        _write(directory / filename, contents)
    return validate(directory)


def validate(directory):
    """Validate explicit reuse of a previously generated credential bundle."""
    directory = Path(directory)
    ca_pem = (directory / "root-ca.pem").read_bytes()
    leaf_pem = (directory / "server.pem").read_bytes()
    ca = x509.load_pem_x509_certificate(ca_pem)
    leaf = x509.load_pem_x509_certificate(leaf_pem)
    ca_key = serialization.load_pem_private_key((directory / "root-ca.key").read_bytes(), None)
    key = serialization.load_pem_private_key((directory / "server.key").read_bytes(), None)
    if not isinstance(ca.public_key(), rsa.RSAPublicKey):
        raise ValueError("Root CA must use RSA")
    if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Root certificate is not a CA")
    if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Server certificate must not be a CA")
    for certificate, signing_key in ((ca, ca.public_key()), (leaf, ca.public_key())):
        signing_key.verify(certificate.signature, certificate.tbs_certificate_bytes,
                           padding.PKCS1v15(), certificate.signature_hash_algorithm)
        before = getattr(certificate, "not_valid_before_utc", None)
        if before is None:
            before = certificate.not_valid_before.replace(tzinfo=timezone.utc)
        after = getattr(certificate, "not_valid_after_utc", None)
        if after is None:
            after = certificate.not_valid_after.replace(tzinfo=timezone.utc)
        if before > NOT_BEFORE or after <= datetime.now(timezone.utc):
            raise ValueError("Certificates must be valid at epoch zero and not expired")
    if leaf.issuer != ca.subject or ca.issuer != ca.subject:
        raise ValueError("Certificate issuer mismatch")
    if (_public(ca_key.public_key()) != _public(ca.public_key()) or
            _public(key.public_key()) != _public(leaf.public_key())):
        raise ValueError("Certificate and private key mismatch")
    if (directory / "server-chain.pem").read_bytes() != leaf_pem + ca_pem:
        raise ValueError("Server chain must contain the matching leaf followed by its root")
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if not set(HOSTNAMES).issubset(names.get_values_for_type(x509.DNSName)):
        raise ValueError("Server certificate does not cover the Skyport hostnames")
    return {"directory": str(directory), "root_sha256": ca.fingerprint(hashes.SHA256()).hex()}
