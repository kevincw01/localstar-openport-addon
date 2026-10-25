import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from credential_fixture import generate, NOT_BEFORE

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def send(conn, obj):
    body = json.dumps(obj).encode()
    conn.sendall(struct.pack('>I', len(body)) + body)


def receive(conn):
    def exact(size):
        buf = b''
        while len(buf) < size:
            data = conn.recv(size - len(buf))
            if not data:
                raise EOFError('Connection ended before complete frame')
            buf += data
        return buf
    return json.loads(exact(struct.unpack('>I', exact(4))[0]))


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.creds = cls.root / 'credentials'
        generate(cls.creds)
        cls.other = cls.root / 'other'
        generate(cls.other)
        cls.entry, cls.session = free_port(), free_port()
        while cls.entry == cls.session:
            cls.session = free_port()
        cls.logfile = (cls.root / 'server.log').open('w+')
        cls.addClassCleanup(cls.logfile.close)
        cls.proc = subprocess.Popen([
            sys.executable, str(ROOT / 'tests/serve_fixture.py'), '--bind', '127.0.0.1',
            '--entry-port', str(cls.entry), '--session-port', str(cls.session),
            '--lat', '0', '--lon', '0', '--tz', 'Europe/London',
            '--cert', str(cls.creds / 'server-chain.pem'), '--key', str(cls.creds / 'server.key'),
        ], cwd=ROOT, stdout=cls.logfile, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop)
        deadline = time.monotonic() + 10
        for port in (cls.entry, cls.session):
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if cls.proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError((cls.root / 'server.log').read_text())
                    time.sleep(.05)

    @classmethod
    def stop(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)

    def connect(self, port=None, ca=None, hostname='connect.skyport.io'):
        context = ssl.create_default_context(cafile=str((ca or self.creds) / 'root-ca.pem'))
        raw = socket.create_connection(('127.0.0.1', port or self.session), timeout=3)
        self.addCleanup(raw.close)
        conn = context.wrap_socket(raw, server_hostname=hostname)
        self.addCleanup(conn.close)
        return conn

    def test_redirect_and_epoch_validity(self):
        conn = self.connect(self.entry)
        leaf = x509.load_der_x509_certificate(conn.getpeercert(binary_form=True))
        before = getattr(leaf, 'not_valid_before_utc', None)
        if before is None:
            before = leaf.not_valid_before.replace(tzinfo=timezone.utc)
        self.assertEqual(before, NOT_BEFORE)
        self.assertEqual(conn.recv(256).decode(), f'connect.skyport.io:{self.session}')

    def test_wrong_installation_root_and_hostname_are_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.connect(ca=self.other)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.connect(hostname='example.invalid')

    def test_login_time_weather_and_every_ping_echo(self):
        conn = self.connect()
        send(conn, {'command': 2, 'mac': '02:00:00:00:00:01', 'fwVer': 'VH6.93'})
        self.assertEqual(receive(conn)['command'], 3)
        send(conn, {'command': 13})
        self.assertEqual(receive(conn), {'command': 14, 'observe': []})
        send(conn, {'command': 45})
        stamp = receive(conn)['timestamp']
        expected = datetime.now(ZoneInfo('Europe/London')).replace(tzinfo=timezone.utc).timestamp() * 1000
        self.assertLess(abs(stamp - expected), 5000)
        # Listener readiness does not imply the background weather fetch has
        # finished, even with a deterministic provider. Exercise the documented
        # initial unavailable response until the fixture cache is populated.
        deadline = time.monotonic() + 5
        while True:
            send(conn, {'command': 59, 'sequence': 100})
            reply = receive(conn)
            self.assertEqual(reply['command'], 60)
            self.assertEqual(reply['sequence'], 100)
            if not reply.get('error'):
                break
            if time.monotonic() >= deadline:
                self.fail('Fixture weather cache did not populate')
            time.sleep(.01)
        payload = json.loads(reply['data'])
        self.assertEqual(payload['current']['temp_f'], 68)
        self.assertEqual(payload['current']['temp_c'], 20)
        self.assertEqual(payload['current']['wind_dir'], 'E')
        self.assertEqual(len(payload['forecast']), 6)
        self.assertEqual(payload['sun_phase']['sunrise'], {'hour': 6, 'minute': 0})
        for sequence in range(201, 213):
            send(conn, {'command': 1, 'sequence': sequence})
            self.assertEqual(receive(conn), {'command': 1, 'sequence': sequence})
        deadline = time.monotonic() + 3
        while True:
            logs = (self.root / 'server.log').read_text()
            if 'PING echo sent to 127.0.0.1 (#12, sequence=212)' in logs:
                break
            if time.monotonic() > deadline:
                self.fail(logs)
            time.sleep(.01)
        for number, sequence in enumerate(range(201, 213), 1):
            self.assertIn(f'PING received from 127.0.0.1 (#{number}, sequence={sequence})', logs)
            self.assertIn(f'PING echo sent to 127.0.0.1 (#{number}, sequence={sequence})', logs)


if __name__ == '__main__':
    unittest.main()
