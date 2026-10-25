#!/usr/bin/env python3
"""Container test for ingress generation, upload, TLS activation and persistence.

Uses an isolated Docker network with the documented Supervisor ingress source IP.
All keys are disposable. Does not connect to a thermostat or production server.
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


def smoke(image, sudo=False):
    prefix = ['sudo', 'docker'] if sudo else ['docker']
    suffix = uuid.uuid4().hex[:10]
    name, network = 'localstar-cert-test-' + suffix, 'localstar-cert-net-' + suffix

    def docker(*args, input=None):
        return subprocess.check_output([*prefix, *args], input=input, text=True, stderr=subprocess.PIPE).strip()

    def proxy(path='api/status', body=None, invalid_token=False):
        code = '''import json,re,urllib.request,urllib.error
base='http://172.30.32.3:8099/'
with urllib.request.urlopen(base,timeout=10) as response: page=response.read().decode()
token=re.search(r'name="csrf-token" content="([^"]+)"',page).group(1)
'''
        code += 'path=' + repr(path) + '\nbody=' + repr(body) + '\n'
        code += 'invalid_token=' + repr(invalid_token) + '\n'
        code += '''
request=urllib.request.Request(base+path)
if body is not None:
 request=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-CSRF-Token':'invalid' if invalid_token else token})
try:
 with urllib.request.urlopen(request,timeout=30) as response:
  print(json.dumps({'status':response.status,'body':response.read().decode()}))
except urllib.error.HTTPError as error:
 print(json.dumps({'status':error.code,'body':error.read().decode()}))
'''
        return json.loads(docker('run', '--rm', '--network', network, '--ip', '172.30.32.2',
                                 '-i', image, 'python3', '-', input=code))

    def tls(ca_file):
        context = ssl.create_default_context(cafile=str(ca_file))
        address = docker('port', name, '41414/tcp')
        host, port = address.rsplit(':', 1)
        raw = socket.create_connection((host, int(port)), timeout=5)
        try:
            return context.wrap_socket(raw, server_hostname='connect.skyport.io')
        except BaseException:
            raw.close()
            raise

    def roundtrip(ca_file):
        with tls(ca_file) as conn:
            send(conn, {'command': 2, 'mac': '02:00:00:00:00:01', 'fwVer': 'VH6.93'})
            assert receive(conn)['command'] == 3
            send(conn, {'command': 1, 'sequence': 17})
            assert receive(conn) == {'command': 1, 'sequence': 17}

    with tempfile.TemporaryDirectory(prefix='localstar-cert-test-') as temp:
        base = Path(temp)
        data, ssl_dir = base / 'data', base / 'ssl'
        data.mkdir()
        ssl_dir.mkdir()
        (data / 'options.json').write_text(json.dumps({
            'latitude': 0, 'longitude': 0, 'timezone': 'Etc/UTC',
            'sync_time': True, 'allow_old_tls': False,
        }))
        fixture = base / 'existing-server'
        generate(fixture)
        created_network = False
        try:
            docker('network', 'create', '--subnet', '172.30.32.0/29', network)
            created_network = True
            docker('run', '--detach', '--name', name, '--network', network, '--ip', '172.30.32.3',
                   '--user', f'{os.getuid()}:{os.getgid()}',
                   '--mount', f'type=bind,source={data},target=/data',
                   '--mount', f'type=bind,source={ssl_dir},target=/ssl,readonly',
                   '-p', '127.0.0.1::9001', '-p', '127.0.0.1::41414', image)
            deadline = time.monotonic() + 20
            while True:
                try:
                    status = proxy()
                    assert status['status'] == 200, status
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            assert not json.loads(status['body'])['has_identity']
            assert list((data / 'certificates').iterdir()) == []
            denied = docker('exec', name, 'python3', '-c',
                            "import urllib.request,urllib.error\ntry: urllib.request.urlopen('http://127.0.0.1:8099/')\nexcept urllib.error.HTTPError as e: print(e.code)")
            assert denied == '403', denied
            assert proxy('api/generate', {}, invalid_token=True)['status'] == 403
            generated = proxy('api/generate', {})
            assert generated['status'] == 200, generated
            generated_status = json.loads(generated['body'])
            assert generated_status['server_running']
            assert generated_status['certificate']['source'] == 'generated'
            root = proxy('root-ca.pem')
            assert root['status'] == 200 and 'PRIVATE KEY' not in root['body']
            ca_file = base / 'downloaded-root.pem'
            ca_file.write_text(root['body'])
            roundtrip(ca_file)
            pointer = (data / 'certificates/current.json').read_bytes()
            docker('restart', '--time', '10', name)
            deadline = time.monotonic() + 20
            while True:
                try:
                    roundtrip(ca_file)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.2)
            assert (data / 'certificates/current.json').read_bytes() == pointer
            assert proxy('api/generate', {})['status'] == 409
            uploaded = proxy('api/upload', {
                'certificate': (fixture / 'server-chain.pem').read_text(),
                'key': (fixture / 'server.key').read_text(), 'replace': True,
            })
            assert uploaded['status'] == 200, uploaded
            imported_status = json.loads(uploaded['body'])
            assert imported_status['certificate']['source'] == 'uploaded'
            assert imported_status['certificate']['fingerprint'] != generated_status['certificate']['fingerprint']
            roundtrip(fixture / 'root-ca.pem')
            # A dead Skyport child is recovered without replacing its stored identity.
            docker('exec', name, 'python3', '-c',
                   "import os,signal,pathlib\nfor p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):\n try: parts=p.read_bytes().split(b'\\0')\n except OSError: continue\n if b'/app/server.py' in parts: os.kill(int(p.parent.name),signal.SIGKILL)")
            deadline = time.monotonic() + 20
            while True:
                try:
                    roundtrip(fixture / 'root-ca.pem')
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.3)
            assert json.loads(proxy()['body'])['certificate']['fingerprint'] == imported_status['certificate']['fingerprint']
            print('PASS: ingress-only access, CSRF, unique generation, public download, persistent restart, existing-pair upload and child recovery')
        except BaseException:
            with __import__('contextlib').suppress(Exception):
                print(docker('logs', name), file=sys.stderr)
            raise
        finally:
            subprocess.run([*prefix, 'rm', '-f', name], capture_output=True, check=False)
            if created_network:
                subprocess.run([*prefix, 'network', 'rm', network], capture_output=True, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='localstar-openport:test')
    parser.add_argument('--sudo', action='store_true')
    args = parser.parse_args()
    smoke(args.image, args.sudo)
