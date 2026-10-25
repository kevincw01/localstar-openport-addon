#!/usr/bin/env python3
"""Exercise real TLS -> child telemetry -> MQTT discovery with a disposable broker."""

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

import aiomqtt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from certificate_store import CertificateStore
from management import ServerManager
from test_server import free_port, send, receive


async def exercise(port):
    with tempfile.TemporaryDirectory(prefix="localstar-mqtt-data-") as temporary:
        data = Path(temporary)
        store = CertificateStore(data / "certificates")
        profile = store.prepare_generated()
        store.activate(profile)
        options = {"latitude": 0, "longitude": 0, "timezone": "Etc/UTC",
                   "certificate_file": str(profile["certificate"]), "private_key_file": str(profile["key"]),
                   "root_ca_file": str(profile["root"]),
                   "mqtt_host": "127.0.0.1", "mqtt_port": port}
        manager = ServerManager(options, store, data / "ssl")
        manager.mqtt.interval = .2
        manager.mqtt.retry = .2
        entry, session = free_port(), free_port()
        while entry == session:
            session = free_port()
        command = [sys.executable, str(ROOT / "tests/serve_fixture.py"),
                   "--bind", "127.0.0.1", "--entry-port", str(entry), "--session-port", str(session),
                   "--lat", "0", "--lon", "0", "--tz", "Etc/UTC",
                   "--cert", str(profile["certificate"]), "--key", str(profile["key"])]
        messages, counts = {}, {}
        connections = []

        async with aiomqtt.Client("127.0.0.1", port=port) as observer:
            await observer.subscribe("homeassistant/#")
            await observer.subscribe("localstar_openport/#")

            async def collect():
                async for message in observer.messages:
                    topic = str(message.topic)
                    messages[topic] = message.payload.decode()
                    counts[topic] = counts.get(topic, 0) + 1

            reader = asyncio.create_task(collect())

            async def wait(predicate):
                deadline = time.monotonic() + 15
                while not predicate():
                    if time.monotonic() > deadline:
                        raise AssertionError("Timed out waiting for MQTT state")
                    await asyncio.sleep(.05)

            base = manager.mqtt.base
            state_topic = base + "/state"
            state = lambda: json.loads(messages.get(state_topic, "{}"))

            def connect():
                ctx = ssl.create_default_context(cafile=str(profile["root"]))
                conn = ctx.wrap_socket(socket.create_connection(("127.0.0.1", session), timeout=3),
                                       server_hostname="connect.skyport.io")
                send(conn, {"command": 2, "mac": "02:00:00:00:00:01", "fwVer": "VH6.93"})
                assert receive(conn)["command"] == 3
                return conn

            try:
                with patch("management.server_command", return_value=command):
                    await manager.start()
                    await wait(lambda: state().get("weather_fresh"))
                    assert state()["temperature"] == 68
                    assert state()["humidity"] == 50
                    assert state()["wind_speed"] == 5
                    assert state()["detected_thermostats"] == 0
                    first = await asyncio.to_thread(connect)
                    connections.append(first)
                    await wait(lambda: state().get("connected_thermostats") == 1)
                    identity = next(iter(state()["thermostats"]))
                    device_topic = f"{base}/thermostat/{identity}/state"
                    await asyncio.to_thread(send, first, {"command": 1, "sequence": 123})
                    assert await asyncio.to_thread(receive, first) == {"command": 1, "sequence": 123}
                    await asyncio.to_thread(send, first, {"command": 59, "sequence": 124})
                    assert (await asyncio.to_thread(receive, first))["command"] == 60
                    await wait(lambda: json.loads(messages.get(device_topic, "{}" )).get("last_weather_response"))
                    assert json.loads(messages[device_topic])["last_ping"]
                    second = await asyncio.to_thread(connect)
                    connections.append(second)
                    await asyncio.sleep(.3)
                    first.close()
                    await asyncio.sleep(.4)
                    assert state()["connected_thermostats"] == 1
                    assert state()["detected_thermostats"] == 1
                    second.close()
                    await wait(lambda: state().get("connected_thermostats") == 0)
                    config_topic = next(topic for topic in messages if topic.endswith("/temperature/config"))
                    before = counts[config_topic]
                    await observer.publish("homeassistant/status", "online", qos=1)
                    await wait(lambda: counts[config_topic] > before)
                    old_pid = manager.process.pid
                    manager.process.kill()
                    await wait(lambda: manager.process is not None and manager.process.pid != old_pid
                               and manager.process.returncode is None)
                    await wait(lambda: state().get("weather_fresh"))
                    assert state()["connected_thermostats"] == 0
                    assert state()["detected_thermostats"] == 1
                    await manager.close()
                    await wait(lambda: messages.get(base + "/availability") == "offline")
                    restored = ServerManager(options, store, data / "ssl")
                    assert restored.telemetry.identity == manager.telemetry.identity
                    assert restored.telemetry.snapshot(False)["detected_thermostats"] == 1
                    assert restored.telemetry.snapshot(False)["connected_thermostats"] == 0
                    print("PASS: real TLS login/ping/weather, MQTT discovery/state/birth, overlapping sessions, child recovery, offline shutdown and persistent identities")
            finally:
                for conn in connections:
                    conn.close()
                await manager.close()
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sudo", action="store_true")
    args = parser.parse_args()
    docker = (["sudo", "-n"] if args.sudo else []) + ["docker"]
    name = "localstar-mqtt-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="localstar-mqtt-broker-") as temporary:
        config = Path(temporary) / "mosquitto.conf"
        config.write_text("listener 1883\nallow_anonymous true\npersistence false\n")
        config.chmod(0o644)
        try:
            subprocess.run([*docker, "run", "-d", "--name", name,
                            "-p", "127.0.0.1::1883", "-v", f"{config}:/mosquitto/config/mosquitto.conf:ro",
                            "--entrypoint", "mosquitto", "eclipse-mosquitto:2", "-c", "/mosquitto/config/mosquitto.conf"],
                           check=True, stdout=subprocess.DEVNULL)
            mapping = subprocess.check_output([*docker, "port", name, "1883/tcp"], text=True).strip()
            port = int(mapping.rsplit(":", 1)[1])
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.05)
            asyncio.run(exercise(port))
        finally:
            subprocess.run([*docker, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
