"""Small Skyport wire helpers used by the Docker smoke test."""

import json
import struct


def send(conn, obj):
    body = json.dumps(obj).encode()
    conn.sendall(struct.pack('>I', len(body)) + body)


def receive(conn):
    def exact(size):
        result = b''
        while len(result) < size:
            chunk = conn.recv(size - len(result))
            if not chunk:
                raise EOFError('Incomplete Skyport frame')
            result += chunk
        return result
    return json.loads(exact(struct.unpack('>I', exact(4))[0]))
