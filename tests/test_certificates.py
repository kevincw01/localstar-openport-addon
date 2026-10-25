import asyncio
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from credential_fixture import generate as fixture_generate
from certificate_store import CertificateStore, NOT_BEFORE, _date, validate_pair
from management import ServerManager, create_application


def options():
    return {'latitude': 0.0, 'longitude': 0.0, 'timezone': 'Etc/UTC',
            'sync_time': True, 'allow_old_tls': False}


class TestCertificateStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = CertificateStore(self.base / 'managed')
        self.fixture = self.base / 'fixture'
        fixture_generate(self.fixture)
        self.chain = (self.fixture / 'server-chain.pem').read_bytes()
        self.key = (self.fixture / 'server.key').read_bytes()
        self.root = (self.fixture / 'root-ca.pem').read_bytes()

    def test_generated_identities_are_unique_epoch_valid_and_persistent(self):
        first = self.store.prepare_generated()
        second = self.store.prepare_generated()
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        root1 = first['root'].read_bytes()
        self.assertNotEqual(root1, second['root'].read_bytes())
        for pem in (root1, first['certificate'].read_bytes()):
            self.assertEqual(_date(x509.load_pem_x509_certificate(pem), 'not_valid_before'), NOT_BEFORE)
        self.store.activate(first)
        reloaded = CertificateStore(self.store.directory).current()
        self.assertEqual(reloaded['id'], first['id'])
        self.assertEqual(reloaded['root'].read_bytes(), root1)
        if __import__('os').name == 'posix':
            self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(first['key'].stat().st_mode & 0o777, 0o600)

    def test_upload_preserves_identity_without_ca_private_key(self):
        profile = self.store.prepare_uploaded(self.chain, self.key)
        self.assertEqual(profile['source'], 'uploaded')
        self.assertEqual(profile['root'].read_bytes(), self.root)
        old = x509.load_pem_x509_certificate(self.chain)
        new = x509.load_pem_x509_certificate(profile['certificate'].read_bytes())
        self.assertEqual(old.serial_number, new.serial_number)
        self.assertFalse((profile['key'].parent / 'root-ca.key').exists())
        validate_pair(profile['certificate'].read_bytes(), profile['key'].read_bytes())

    def test_leaf_only_upload_can_optionally_supply_root(self):
        leaf = (self.fixture / 'server.pem').read_bytes()
        no_root = self.store.prepare_uploaded(leaf, self.key)
        self.assertFalse(no_root['root_available'])
        self.assertFalse(no_root['root'].exists())
        with_root = self.store.prepare_uploaded(leaf, self.key, self.root)
        self.assertTrue(with_root['root_available'])
        self.assertEqual(with_root['certificate'].read_bytes(), self.chain)

    def test_mismatch_and_private_material_are_rejected_without_changing_active(self):
        active = self.store.prepare_uploaded(self.chain, self.key)
        self.store.activate(active)
        other = self.store.prepare_generated()
        for chain, key, root in [(self.chain, other['key'].read_bytes(), None),
                                 (self.chain + self.key, self.key, None),
                                 (self.chain, self.key + self.key, None),
                                 (self.chain, self.key, other['root'].read_bytes())]:
            with self.subTest(root=bool(root)), self.assertRaises(ValueError):
                self.store.prepare_uploaded(chain, key, root)
            self.assertEqual(self.store.current()['id'], active['id'])

    def test_import_accepts_currently_valid_older_certificates(self):
        # Imported credentials need not start in 1970; users must retain their
        # working pair during migration, even if it has a later notBefore.
        from cryptography.hazmat.primitives import hashes
        key = serialization.load_pem_private_key((self.fixture / 'root-ca.key').read_bytes(), None)
        cert = x509.load_pem_x509_certificate(self.chain)
        builder = (x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer)
                   .public_key(cert.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(datetime(2014, 1, 1, tzinfo=timezone.utc))
                   .not_valid_after(_date(cert, 'not_valid_after')))
        for extension in cert.extensions:
            builder = builder.add_extension(extension.value, extension.critical)
        leaf = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        profile = self.store.prepare_uploaded(leaf + self.root, self.key)
        self.assertTrue(profile['not_before'].startswith('2014-'))


class FakeManager(ServerManager):
    """Keep HTTP tests independent of fixed production listener ports."""
    async def start(self):
        self.profile = self.store.current()

    async def close(self):
        pass

    async def activate(self, profile):
        self.store.activate(profile)
        self.profile = profile


class TestIngress(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = CertificateStore(self.base / 'managed')
        self.manager = FakeManager(options(), self.store, self.base / 'ssl')
        self.client = TestClient(TestServer(create_application(
            options(), manager=self.manager, trusted_peers=('127.0.0.1',))))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        response = await self.client.get('/')
        self.page = await response.text()
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', self.page).group(1)
        self.headers = {'X-CSRF-Token': self.token}

    async def test_generate_download_and_explicit_replacement(self):
        response = await self.client.post('/api/generate', json={}, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        status = await response.json()
        self.assertEqual(status['certificate']['source'], 'generated')
        self.assertNotIn('PRIVATE KEY', json.dumps(status))
        download = await self.client.get('/root-ca.pem')
        root = await download.read()
        self.assertEqual(download.status, 200)
        self.assertTrue(root.startswith(b'-----BEGIN CERTIFICATE-----'))
        self.assertNotIn(b'PRIVATE KEY', root)
        previous = self.store.current()['id']
        response = await self.client.post('/api/generate', json={}, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.store.current()['id'], previous)
        response = await self.client.post('/api/generate', json={'replace': True}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertNotEqual(self.store.current()['id'], previous)
        for path in ('/server.key', '/root-ca.key', '/api/private-key'):
            self.assertEqual((await self.client.get(path)).status, 404)

    async def test_upload_and_reject_mismatched_pair(self):
        fixture = self.base / 'fixture'
        await asyncio.to_thread(fixture_generate, fixture)
        body = {'certificate': (fixture / 'server-chain.pem').read_text(),
                'key': (fixture / 'server.key').read_text()}
        response = await self.client.post('/api/upload', json=body, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())['certificate']['source'], 'uploaded')
        previous = self.store.current()['id']
        other = await asyncio.to_thread(self.store.prepare_generated)
        body.update(key=other['key'].read_text(), replace=True)
        response = await self.client.post('/api/upload', json=body, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.store.current()['id'], previous)

    async def test_ingress_and_csrf_are_required_and_body_is_limited(self):
        response = await self.client.post('/api/generate', json={})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/upload', json={'certificate': 'x' * 270000}, headers=self.headers)
        self.assertEqual(response.status, 413)
        denied = TestClient(TestServer(create_application(options(), manager=self.manager)))
        await denied.start_server()
        try:
            self.assertEqual((await denied.get('/')).status, 403)
            self.assertEqual((await denied.get('/api/status')).status, 403)
        finally:
            await denied.close()

    async def test_ingress_prefix_and_static_assets(self):
        response = await self.client.get('/', headers={'X-Ingress-Path': '/api/hassio_ingress/example'})
        self.assertIn('<base href="/api/hassio_ingress/example/">', await response.text())
        response = await self.client.get('/static/app.js')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')


class TestManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = CertificateStore(self.base / 'managed')
        self.manager = ServerManager(options(), self.store, self.base / 'ssl')
        self.addAsyncCleanup(self.manager.close)

    async def test_first_start_waits_for_upload_without_creating_keys(self):
        with contextlib.redirect_stdout(__import__('io').StringIO()):
            await self.manager.start()
        self.assertIsNone(self.manager.profile)
        self.assertIsNone(self.manager.process)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    async def test_failed_activation_keeps_previous_identity(self):
        previous = await asyncio.to_thread(self.store.prepare_generated)
        proposed = await asyncio.to_thread(self.store.prepare_generated)
        self.store.activate(previous)
        self.manager.profile = previous
        launches = []

        async def launch(profile):
            launches.append(profile['id'])
            if profile is proposed:
                raise RuntimeError('Simulated listener failure')

        with patch.object(self.manager, '_launch', side_effect=launch):
            with self.assertRaises(RuntimeError):
                await self.manager.activate(proposed)
        self.assertEqual(launches, [proposed['id'], previous['id']])
        self.assertEqual(self.store.current()['id'], previous['id'])
        self.assertEqual(self.manager.profile['id'], previous['id'])

    async def test_persistent_profile_starts_same_identity(self):
        profile = await asyncio.to_thread(self.store.prepare_generated)
        self.store.activate(profile)
        with patch.object(self.manager, '_launch', new_callable=AsyncMock) as launch:
            await self.manager.start()
            self.assertEqual(launch.call_args.args[0]['id'], profile['id'])
        self.assertEqual(self.store.current()['id'], profile['id'])
