import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from supervisor_options import save_certificate_paths


class TestOptionsWriter(unittest.IsolatedAsyncioTestCase):
    async def test_local_options_keep_unrelated_settings_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            path = Path(directory)/'options.json'
            path.write_text(json.dumps({'timezone': 'Home Assistant', 'mqtt_password': 'disposable'}))
            changes = {'certificate_file': '/data/certificates/example/server-chain.pem',
                       'private_key_file': '/data/certificates/example/server.key'}
            await save_certificate_paths(directory, changes)
            saved = json.loads(path.read_text())
            self.assertEqual(saved, {'timezone': 'Home Assistant', 'mqtt_password': 'disposable', **changes})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_supervisor_write_preserves_other_options_and_authenticates(self):
        saved = {}

        async def info(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer disposable-token')
            return web.json_response({'result': 'ok', 'data': {'options': {
                'timezone': 'Home Assistant', 'mqtt_password': '!secret broker_password',
                'certificate_file': '', 'private_key_file': ''}}})

        async def update(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer disposable-token')
            saved.update(await request.json())
            return web.json_response({'result': 'ok', 'data': {}})

        app = web.Application()
        app.router.add_get('/addons/self/info', info)
        app.router.add_post('/addons/self/options', update)
        async with TestServer(app) as server:
            with patch('supervisor_options.SUPERVISOR_URL', str(server.make_url('')).rstrip('/')), \
                    patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'disposable-token'}):
                changes = {'certificate_file': '/data/certificates/example/server-chain.pem',
                           'private_key_file': '/data/certificates/example/server.key'}
                await save_certificate_paths('/unused', changes)
        self.assertEqual(saved, {'options': {'timezone': 'Home Assistant',
            'mqtt_password': '!secret broker_password', **changes}})

    async def test_failure_does_not_include_sensitive_response(self):
        async def unavailable(request):
            return web.Response(status=500, text='PRIVATE-response-do-not-log')
        app = web.Application()
        app.router.add_get('/addons/self/info', unavailable)
        async with TestServer(app) as server:
            with patch('supervisor_options.SUPERVISOR_URL', str(server.make_url('')).rstrip('/')), \
                    patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'disposable-token'}):
                with self.assertRaises(RuntimeError) as caught:
                    await save_certificate_paths('/unused', {'certificate_file': '', 'private_key_file': ''})
        self.assertNotIn('PRIVATE-response', str(caught.exception))

    async def test_writer_rejects_other_configuration_mutations(self):
        with self.assertRaises(ValueError):
            await save_certificate_paths('/unused', {'mqtt_password': 'disposable'})
