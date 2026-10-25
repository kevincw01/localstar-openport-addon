#!/usr/bin/env python3
"""Build first: docker build -t localstar-openport:test .

Run: python3 tests/smoke_addon.py [--sudo] [--image localstar-openport:test]
Uses disposable credentials, loopback-only Docker ports and actual Open-Meteo.
No thermostat or production configuration is touched. Container is always removed.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credential_fixture import generate
from tests_helpers import receive, send


def smoke(image, sudo=False, skip_weather=False):
    prefix = ['sudo', 'docker'] if sudo else ['docker']
    name = 'localstar-openport-test-' + uuid.uuid4().hex[:12]

    def docker(*args):
        return subprocess.check_output([*prefix, *args], text=True).strip()

    with tempfile.TemporaryDirectory(prefix='localstar-addon-test-') as temp:
        root = Path(temp)
        data, ssl_dir = root / 'data', root / 'ssl'
        data.mkdir()
        ssl_dir.mkdir()
        credentials = ssl_dir / 'localstar-openport'
        generate(credentials)
        before = {p.name: p.read_bytes() for p in credentials.iterdir()}
        (data / 'options.json').write_text(json.dumps({
            'latitude': 0, 'longitude': 0, 'timezone': 'Etc/UTC',
            'certificate_file': 'localstar-openport/server-chain.pem',
            'private_key_file': 'localstar-openport/server.key',
            'root_ca_file': 'localstar-openport/root-ca.pem',
            'sync_time': True, 'allow_old_tls': False,
        }))
        context = ssl.create_default_context(cafile=str(credentials / 'root-ca.pem'))
        try:
            docker('run', '--detach', '--name', name,
                   '--user', f'{os.getuid()}:{os.getgid()}',
                   '--mount', f'type=bind,source={data},target=/data',
                   '--mount', f'type=bind,source={ssl_dir},target=/ssl,readonly',
                   '-p', '127.0.0.1::9001', '-p', '127.0.0.1::41414', image)

            def connect(container_port):
                address = docker('port', name, f'{container_port}/tcp')
                host, port = address.rsplit(':', 1)
                raw = socket.create_connection((host, int(port)), timeout=5)
                try:
                    return context.wrap_socket(raw, server_hostname='connect.skyport.io')
                except BaseException:
                    raw.close()
                    raise

            deadline = time.monotonic() + 30
            while True:
                try:
                    with connect(9001) as conn:
                        assert conn.recv(256) == b'connect.skyport.io:41414'
                    break
                except (OSError, ssl.SSLError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(docker('logs', name))
                    time.sleep(.2)
            with connect(41414) as conn:
                send(conn, {'command': 2, 'mac': '02:00:00:00:00:01', 'fwVer': 'VH6.93'})
                assert receive(conn)['command'] == 3
                send(conn, {'command': 45})
                assert abs(receive(conn)['timestamp'] - time.time() * 1000) < 5000
                for sequence in (1, 2):
                    send(conn, {'command': 1, 'sequence': sequence})
                    assert receive(conn) == {'command': 1, 'sequence': sequence}
                if not skip_weather:
                    deadline = time.monotonic() + 45
                    while True:
                        send(conn, {'command': 59, 'sequence': 3})
                        reply = receive(conn)
                        assert reply['command'] == 60 and reply['sequence'] == 3
                        if not reply.get('error'):
                            payload = json.loads(reply['data'])
                            assert 'temp_f' in payload['current'] and len(payload['forecast']) >= 5
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Weather unavailable: ' + docker('logs', name))
                        time.sleep(1)
            logs = docker('logs', name)
            for text in ('PING received', 'PING echo sent', '#1, sequence=1', '#2, sequence=2'):
                assert text in logs, logs
            docker('stop', '--time', '10', name)
            state = json.loads(docker('inspect', name))[0]['State']
            assert state['ExitCode'] == 0, state
            docker('start', name)
            # Prove the process can restart using the exact same credential bundle.
            deadline = time.monotonic() + 15
            while True:
                try:
                    with connect(9001) as conn:
                        assert conn.recv(256) == b'connect.skyport.io:41414'
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            assert before == {p.name: p.read_bytes() for p in credentials.iterdir()}
            print('PASS: container startup, TLS, login, time, every ping/echo, stop and restart')
            print('Live weather fetch: ' + ('skipped (weather mapping covered by unit tests)' if skip_weather else 'PASS'))
        finally:
            subprocess.run([*prefix, 'rm', '-f', name], capture_output=True, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sudo', action='store_true')
    parser.add_argument('--image', default='localstar-openport:test')
    parser.add_argument('--skip-weather', action='store_true',
                        help='Skip live provider access in CI; unit tests cover weather deterministically')
    args = parser.parse_args()
    smoke(args.image, args.sudo, args.skip_weather)
