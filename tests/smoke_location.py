#!/usr/bin/env python3
"""Verify initial HA location is pinned until explicitly edited by the user."""

import argparse
from datetime import datetime, timezone
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
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credential_fixture import generate
from tests_helpers import send, receive

PROXY = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path != '/core/api/config' or self.headers.get('Authorization') != 'Bearer disposable-token':
   self.send_error(403); return
  body=Path('/fixture/home.json').read_bytes()
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
HTTPServer(('0.0.0.0',80),Handler).serve_forever()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sudo', action='store_true')
    parser.add_argument('--image', default='localstar-openport:test')
    args = parser.parse_args()
    prefix = (['sudo', '-n'] if args.sudo else []) + ['docker']
    suffix = uuid.uuid4().hex[:10]
    app, proxy, network = ('localstar-location-' + suffix + tail for tail in ('-app', '-proxy', '-net'))

    def docker(*command):
        return subprocess.check_output([*prefix, *command], text=True).strip()

    with tempfile.TemporaryDirectory(prefix='localstar-location-test-') as directory:
        root = Path(directory)
        data, ssl_dir, fixture = (root / name for name in ('data', 'ssl', 'fixture'))
        for path in (data, ssl_dir, fixture):
            path.mkdir()
        credentials = ssl_dir / 'localstar-openport'
        generate(credentials)
        before = {path.name: path.read_bytes() for path in credentials.iterdir()}
        (data / 'options.json').write_text(json.dumps({'timezone': 'Home Assistant', 'mqtt_enabled': False}))
        home = {'latitude': 0, 'longitude': 0, 'time_zone': 'Etc/UTC'}
        (fixture / 'home.json').write_text(json.dumps(home))
        context = ssl.create_default_context(cafile=str(credentials / 'root-ca.pem'))
        created_network = False
        try:
            docker('network', 'create', network)
            created_network = True
            docker('run', '-d', '--name', proxy, '--network', network, '--network-alias', 'supervisor',
                   '--mount', f'type=bind,source={fixture},target=/fixture,readonly',
                   args.image, 'python3', '-u', '-c', PROXY)
            docker('run', '-d', '--name', app, '--network', network,
                   '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'SUPERVISOR_TOKEN=disposable-token',
                   '--mount', f'type=bind,source={data},target=/data',
                   '--mount', f'type=bind,source={ssl_dir},target=/ssl,readonly',
                   '-p', '127.0.0.1::9001', '-p', '127.0.0.1::41414', args.image)

            def roundtrip(zone):
                host, port = docker('port', app, '41414/tcp').rsplit(':', 1)
                deadline = time.monotonic() + 45
                while True:
                    try:
                        with socket.create_connection((host, int(port)), timeout=2) as raw, \
                                context.wrap_socket(raw, server_hostname='connect.skyport.io') as conn:
                            send(conn, {'command': 2, 'mac': '02:00:00:00:00:01', 'fwVer': 'VH6.93'})
                            assert receive(conn)['command'] == 3
                            send(conn, {'command': 45})
                            actual = receive(conn)['timestamp']
                            expected = datetime.now(ZoneInfo(zone)).replace(tzinfo=timezone.utc).timestamp() * 1000
                            assert abs(actual - expected) < 5000
                        return
                    except OSError:
                        if time.monotonic() > deadline:
                            raise RuntimeError(docker('logs', app))
                        time.sleep(.2)

            roundtrip('Etc/UTC')
            assert json.loads((data / 'location.json').read_text()) == {
                'latitude': 0, 'longitude': 0, 'timezone': 'Etc/UTC'}
            assert (data / 'location.json').stat().st_mode & 0o777 == 0o600
            home.update(latitude=1, time_zone='Europe/London')
            (fixture / 'home.json').write_text(json.dumps(home))
            docker('restart', '--time', '10', app)
            roundtrip('Etc/UTC')
            assert json.loads((data / 'location.json').read_text())['latitude'] == 0
            (data / 'options.json').write_text(json.dumps({
                'latitude': 1, 'timezone': 'Europe/London', 'mqtt_enabled': False}))
            docker('restart', '--time', '10', app)
            roundtrip('Europe/London')
            cached = (data / 'location.json').read_bytes()
            assert json.loads(cached)['latitude'] == 1
            docker('stop', '--time', '5', proxy)
            docker('restart', '--time', '10', app)
            roundtrip('Europe/London')
            assert (data / 'location.json').read_bytes() == cached
            assert 'Using saved location and timezone.' in docker('logs', app)
            assert {path.name: path.read_bytes() for path in credentials.iterdir()} == before
            print('PASS: initial HA import, static settings despite HA changes, explicit edits and Core-outage restart; credentials unchanged')
        finally:
            for container in (app, proxy):
                subprocess.run([*prefix, 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if created_network:
                subprocess.run([*prefix, 'network', 'rm', network], stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
