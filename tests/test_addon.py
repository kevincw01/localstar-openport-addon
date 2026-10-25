"""Validate add-on configuration and lifecycle without a running Supervisor."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from credential_fixture import generate
import run_addon

ROOT = Path(__file__).resolve().parents[1]


class TestAddon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.ssl_dir = cls.root / 'ssl'
        cls.ssl_dir.mkdir()
        cls.credentials = cls.ssl_dir / 'localstar-openport'
        generate(cls.credentials)

    def options(self):
        return {'latitude': 0.0, 'longitude': 0.0, 'timezone': 'Etc/UTC',
                'certificate_file': 'localstar-openport/server-chain.pem',
                'private_key_file': 'localstar-openport/server.key',
                'sync_time': True, 'allow_old_tls': False}

    def test_metadata_and_ui_schema_match_runtime_options(self):
        config = yaml.safe_load((ROOT / 'config.yaml').read_text())
        repository = yaml.safe_load((ROOT / 'repository.yaml').read_text())
        translations = yaml.safe_load((ROOT / 'translations/en.yaml').read_text())
        self.assertEqual(config['name'], 'localstar-openport')
        self.assertEqual(repository['name'], 'localstar-openport')
        self.assertEqual(set(config['schema']), set(self.options()))
        self.assertEqual(set(translations['configuration']), set(self.options()))
        self.assertEqual(set(config['arch']), {'amd64', 'aarch64'})
        self.assertEqual(config['ports'], {'9001/tcp': 9001, '41414/tcp': 41414})
        self.assertEqual(config['startup'], 'services')
        self.assertEqual(config['boot'], 'auto')
        self.assertEqual(config['map'], [{'type': 'ssl', 'read_only': True}])
        for key in ('latitude', 'longitude', 'timezone'):
            self.assertIsNone(config['options'][key])
        for key in ('hassio_api', 'homeassistant_api', 'docker_api', 'full_access', 'host_network'):
            self.assertFalse(config.get(key, False))

    def test_options_map_to_arguments_and_preserve_certificates(self):
        before = {p.name: p.read_bytes() for p in self.credentials.iterdir()}
        command = run_addon.command_from_options(self.options(), self.ssl_dir)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[command.index('--lat') + 1], '0.0')
        self.assertEqual(command[command.index('--tz') + 1], 'Etc/UTC')
        self.assertEqual(command[command.index('--cert') + 1], str(self.credentials / 'server-chain.pem'))
        self.assertEqual(command[command.index('--session-port') + 1], '41414')
        self.assertNotIn('--allow-old-tls', command)
        self.assertNotIn('--no-time-sync', command)
        options = self.options() | {'sync_time': False, 'allow_old_tls': True}
        command = run_addon.command_from_options(options, self.ssl_dir)
        self.assertIn('--no-time-sync', command)
        self.assertIn('--allow-old-tls', command)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.credentials.iterdir()})

    def test_invalid_location_and_boolean_options_are_rejected(self):
        for key, value in [('latitude', None), ('latitude', True), ('latitude', float('nan')),
                           ('latitude', 91), ('longitude', -181), ('longitude', '0'),
                           ('timezone', None), ('timezone', 'invalid/timezone'),
                           ('sync_time', 'false'), ('allow_old_tls', 'false')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                run_addon.command_from_options(self.options() | {key: value}, self.ssl_dir)

    def test_certificate_paths_cannot_escape_ssl_mount(self):
        outside = self.root / 'outside.pem'
        outside.write_text('not a certificate')
        for filename in ('../outside.pem', str(outside)):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                run_addon.command_from_options(self.options() | {'certificate_file': filename}, self.ssl_dir)
        if hasattr(Path, 'symlink_to') and sys.platform != 'win32':
            link = self.ssl_dir / 'outside-link.pem'
            link.symlink_to(outside)
            with self.assertRaises(ValueError):
                run_addon.command_from_options(self.options() | {'certificate_file': link.name}, self.ssl_dir)

    def test_missing_or_mismatched_keys_fail_without_generating_credentials(self):
        missing = 'missing/server-chain.pem'
        with self.assertRaises(FileNotFoundError):
            run_addon.command_from_options(self.options() | {'certificate_file': missing}, self.ssl_dir)
        self.assertFalse((self.ssl_dir / 'missing').exists())
        other = self.ssl_dir / 'other'
        generate(other)
        with self.assertRaises(ssl.SSLError):
            run_addon.command_from_options(self.options() | {'private_key_file': 'other/server.key'}, self.ssl_dir)

    def test_entrypoint_execs_server_and_reports_bad_options(self):
        path = self.root / 'options.json'
        path.write_text(json.dumps(self.options()))
        original = run_addon.command_from_options
        with patch.object(run_addon, 'command_from_options',
                          side_effect=lambda options: original(options, self.ssl_dir)), \
                patch.object(run_addon.os, 'execv') as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_addon.main(path), 0)
            argv = execute.call_args.args[1]
            self.assertEqual(execute.call_args.args[0], sys.executable)
            self.assertIn('--cert', argv)
        path.write_text('{invalid json')
        with contextlib.redirect_stderr(io.StringIO()) as stderr, patch.object(run_addon.os, 'execv') as execute:
            self.assertEqual(run_addon.main(path), 1)
            execute.assert_not_called()
            self.assertIn('configuration error', stderr.getvalue())

    def test_either_occupied_port_fails_whole_startup_and_releases_other_port(self):
        for occupied_kind in ('entry', 'session'):
            with self.subTest(occupied=occupied_kind), socket.socket() as occupied, socket.socket() as available:
                occupied.bind(('127.0.0.1', 0))
                occupied.listen(1)
                available.bind(('127.0.0.1', 0))
                free_port = available.getsockname()[1]
                available.close()
                used_port = occupied.getsockname()[1]
                entry, session = (used_port, free_port) if occupied_kind == 'entry' else (free_port, used_port)
                result = subprocess.run([
                    sys.executable, str(ROOT / 'server.py'),
                    '--bind', '127.0.0.1', '--entry-port', str(entry), '--session-port', str(session),
                    '--cert', str(self.credentials / 'server-chain.pem'),
                    '--key', str(self.credentials / 'server.key'),
                    '--lat', '0', '--lon', '0', '--tz', 'Etc/UTC',
                ], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot run Skyport listeners', result.stderr)
                self.assertNotIn('weather updated', result.stdout)
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', free_port))


if __name__ == '__main__':
    unittest.main()
