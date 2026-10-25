"""Validate, generate, and atomically activate persistent server credentials."""

import os
import json
import re
import shutil
import uuid
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
        stream.flush()
        os.fsync(stream.fileno())


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
    return directory


def _date(certificate, field):
    value = getattr(certificate, field + '_utc', None)
    return value if value is not None else getattr(certificate, field).replace(tzinfo=timezone.utc)


def certificates(pem):
    """Accept only PEM certificate blocks, with no hidden private key material."""
    if not isinstance(pem, bytes) or len(pem) > 128 * 1024:
        raise ValueError('Supply a PEM certificate or chain smaller than 128 KiB')
    blocks = re.findall(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', pem, re.S)
    remainder = re.sub(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', b'', pem, flags=re.S)
    if not blocks or remainder.strip():
        raise ValueError('Certificate input must contain only PEM CERTIFICATE blocks')
    return [x509.load_pem_x509_certificate(block) for block in blocks]


def validate_pair(chain_pem, key_pem, root_pem=None):
    """Validate uploaded server material without requiring the private CA key."""
    chain = certificates(chain_pem)
    if not isinstance(key_pem, bytes) or len(key_pem) > 32 * 1024:
        raise ValueError('Supply an unencrypted PEM private key smaller than 32 KiB')
    if (key_pem.count(b'-----BEGIN') != 1 or key_pem.count(b'-----END') != 1 or
            not re.fullmatch(rb'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?-----END (?:RSA |EC )?PRIVATE KEY-----', key_pem.strip(), re.S)):
        raise ValueError('Private key input must contain one unencrypted PEM key block')
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError('Private key must be valid, unencrypted PEM') from exc
    if _public(key.public_key()) != _public(chain[0].public_key()):
        raise ValueError('The private key does not match the server certificate')
    leaf = chain[0]
    try:
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError('The first certificate must be a server leaf, not a CA')
    except x509.ExtensionNotFound:
        pass
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if not {'connect.skyport.io', '*.skyport.io'}.intersection(names.get_values_for_type(x509.DNSName)):
            raise ValueError('Server certificate must cover connect.skyport.io')
    except x509.ExtensionNotFound as exc:
        raise ValueError('Server certificate must have a Skyport DNS subject alternative name') from exc
    try:
        usages = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if ExtendedKeyUsageOID.SERVER_AUTH not in usages:
            raise ValueError('Certificate is not valid for TLS server authentication')
    except x509.ExtensionNotFound:
        pass
    if root_pem:
        roots = certificates(root_pem)
        if len(roots) != 1:
            raise ValueError('Supply exactly one public root CA certificate')
        root = roots[0]
        if root.fingerprint(hashes.SHA256()) != chain[-1].fingerprint(hashes.SHA256()):
            chain.append(root)
    now = datetime.now(timezone.utc)
    for cert in chain:
        if not _date(cert, 'not_valid_before') <= now <= _date(cert, 'not_valid_after'):
            raise ValueError('The uploaded certificate chain is not currently valid')
    for child, issuer in zip(chain, chain[1:]):
        try:
            if not issuer.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise ValueError('An issuer in the chain is not a CA')
            child.verify_directly_issued_by(issuer)
        except Exception as exc:
            raise ValueError('The certificates do not form a valid signed chain') from exc
    root = chain[-1] if len(chain) > 1 and chain[-1].subject == chain[-1].issuer else None
    if root is not None:
        try:
            root.verify_directly_issued_by(root)
        except Exception as exc:
            raise ValueError('Invalid self-signed root CA') from exc
    elif root_pem:
        raise ValueError('The supplied root CA must be self-signed')
    normalized = b''.join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain)
    metadata = {
        'fingerprint': leaf.fingerprint(hashes.SHA256()).hex(),
        'not_before': _date(leaf, 'not_valid_before').isoformat(),
        'expires': _date(leaf, 'not_valid_after').isoformat(),
        'root_available': root is not None,
    }
    # Serialize only the matching server key, never incidental trailing material.
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return normalized, key_pem, root.public_bytes(serialization.Encoding.PEM) if root else None, metadata


class CertificateStore:
    """Versioned private storage with an atomic active-profile pointer."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    def current(self):
        pointer = self.directory / 'current.json'
        if not pointer.exists():
            return None
        data = json.loads(pointer.read_text())
        identity = data.get('id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid stored certificate profile identifier')
        path = self.directory / identity
        if path.is_symlink() or not path.is_dir():
            raise ValueError('Stored certificate profile is missing or invalid')
        return self.profile(path)

    def profile(self, path):
        path = Path(path)
        metadata = json.loads((path / 'metadata.json').read_text())
        return metadata | {'id': path.name, 'certificate': path / 'server-chain.pem',
                           'key': path / 'server.key', 'root': path / 'root-ca.pem'}

    def prepare_generated(self):
        path = self.directory / uuid.uuid4().hex
        try:
            generate(path)
            _, _, _, metadata = validate_pair((path / 'server-chain.pem').read_bytes(),
                                               (path / 'server.key').read_bytes())
            _write(path / 'metadata.json', json.dumps(metadata | {'source': 'generated'}).encode())
            return self.profile(path)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise

    def prepare_uploaded(self, chain, key, root=None):
        chain, key, root, metadata = validate_pair(chain, key, root)
        path = self.directory / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        try:
            _write(path / 'server-chain.pem', chain)
            _write(path / 'server.key', key)
            if root is not None:
                _write(path / 'root-ca.pem', root)
            _write(path / 'metadata.json', json.dumps(metadata | {'source': 'uploaded'}).encode())
            return self.profile(path)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise

    def activate(self, profile):
        # Profiles are immutable. Only switch the pointer after server startup succeeds.
        temporary = self.directory / (uuid.uuid4().hex + '.tmp')
        try:
            _write(temporary, json.dumps({'id': profile['id']}).encode())
            os.replace(temporary, self.directory / 'current.json')
        finally:
            temporary.unlink(missing_ok=True)

    def discard(self, profile):
        current = self.current()
        if not current or current['id'] != profile['id']:
            shutil.rmtree(self.directory / profile['id'], ignore_errors=True)

