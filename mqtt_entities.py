"""Optional MQTT discovery bridge; broker failures never stop Skyport."""

import asyncio
import contextlib
import json
import os
import ssl

import aiomqtt
from aiohttp import ClientSession, ClientTimeout

VERSION = "1.2.2"
URL = "https://github.com/kevincw01/localstar-openport-addon"

# key, name, device class, unit; weather sensors use the very same cached
# values served to thermostats, with no additional Open-Meteo requests.
WEATHER = (
    ("temperature", "Outdoor temperature", "temperature", "°F"),
    ("humidity", "Outdoor humidity", "humidity", "%"),
    ("wind_speed", "Wind speed", "wind_speed", "mph"),
    ("wind_direction", "Wind direction", None, None),
    ("condition", "Weather condition", None, None),
    ("high", "Today's high", "temperature", "°F"),
    ("low", "Today's low", "temperature", "°F"),
    ("precipitation_probability", "Today's precipitation probability", None, "%"),
)


def discovery(identity, snapshot):
    """Return retained config topics with stable instance/device identifiers."""
    base = f"localstar_openport/{identity}"
    server_id = f"localstar_openport_{identity}"
    server_device = {"identifiers": [server_id], "name": "localstar-openport",
                     "manufacturer": "localstar-openport", "model": "Local Skyport server",
                     "sw_version": VERSION}
    configs = {}

    def entity(component, key, name, *, thermostat=None, device_class=None, unit=None,
               diagnostic=False, weather=False, measurement=False):
        identifier = server_id + (f"_{thermostat}" if thermostat else "")
        topic = f"{base}/thermostat/{thermostat}/state" if thermostat else f"{base}/state"
        device = server_device
        if thermostat:
            device = {"identifiers": [identifier], "name": f"Skyport thermostat {thermostat[-6:]}",
                      "manufacturer": "Venstar", "model": "Skyport client",
                      "via_device": server_id,
                      "sw_version": snapshot["thermostats"][thermostat].get("firmware", "")}
        config = {"name": name, "unique_id": f"{identifier}_{key}", "device": device,
                  "origin": {"name": "localstar-openport", "sw_version": VERSION,
                             "support_url": URL},
                  "state_topic": topic, "value_template": "{{ value_json." + key + " }}",
                  "availability": [{"topic": f"{base}/availability"}], "expire_after": 90}
        if weather:
            config["availability"].append({"topic": f"{base}/state", "value_template":
                "{{ 'online' if value_json.weather_fresh else 'offline' }}"})
            config["availability_mode"] = "all"
        if component == "binary_sensor":
            config["value_template"] = "{{ 'ON' if value_json." + key + " else 'OFF' }}"
            config.update(payload_on="ON", payload_off="OFF")
        if diagnostic:
            config["entity_category"] = "diagnostic"
        if device_class:
            config["device_class"] = device_class
        if unit:
            config["unit_of_measurement"] = unit
        if measurement:
            config["state_class"] = "measurement"
        if thermostat and key == "connected":
            config.update(json_attributes_topic=topic,
                          json_attributes_template="{{ {'address': value_json.address, 'firmware': value_json.firmware} | tojson }}")
        if key == "detected_thermostats":
            config.update(json_attributes_topic=topic,
                          json_attributes_template="{{ {'thermostats': value_json.thermostats} | tojson }}")
        configs[f"homeassistant/{component}/{identifier}/{key}/config"] = config

    for key, name, device_class, unit in WEATHER:
        entity("sensor", key, name, device_class=device_class, unit=unit,
               weather=True, measurement=unit is not None)
    entity("binary_sensor", "server_running", "Skyport server", device_class="running", diagnostic=True)
    entity("binary_sensor", "weather_problem", "Weather fetch problem", device_class="problem", diagnostic=True)
    for key, name in (("detected_thermostats", "Detected thermostats"),
                      ("connected_thermostats", "Connected thermostats")):
        entity("sensor", key, name, diagnostic=True)
    entity("sensor", "weather_updated", "Weather last updated", device_class="timestamp", diagnostic=True)
    entity("sensor", "weather_age", "Weather cache age", device_class="duration", unit="s", diagnostic=True)
    entity("sensor", "certificate_expires", "Server certificate expiry", device_class="timestamp", diagnostic=True)
    for identity in snapshot["thermostats"]:
        entity("binary_sensor", "connected", "Skyport connection", thermostat=identity,
               device_class="connectivity", diagnostic=True)
        for key, name in (("last_seen", "Last seen"), ("last_ping", "Last successful ping echo"),
                          ("last_weather_response", "Last weather response")):
            entity("sensor", key, name, thermostat=identity, device_class="timestamp", diagnostic=True)
    return configs


async def broker_settings(options):
    if options.get("mqtt_host"):
        return {"host": options["mqtt_host"], "port": options.get("mqtt_port", 1883),
                "username": options.get("mqtt_username") or None,
                "password": options.get("mqtt_password") or None,
                "ssl": options.get("mqtt_tls", False)}
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("No Supervisor MQTT service configured")
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        async with session.get("http://supervisor/services/mqtt",
                               headers={"Authorization": f"Bearer {token}"}) as response:
            response.raise_for_status()
            result = await response.json()
            if result.get("result") != "ok":
                raise RuntimeError("MQTT service is unavailable")
            return result["data"]


class MqttEntities:
    def __init__(self, manager, *, interval=30, retry=30):
        self.manager = manager
        self.telemetry = manager.telemetry
        self.interval, self.retry = interval, retry
        self.base = f"localstar_openport/{self.telemetry.identity}"
        self.status = "disabled" if not manager.options.get("mqtt_enabled", True) else "waiting"
        self.task = None
        self.wake = asyncio.Event()
        self.announced = {}

    def start(self):
        if self.manager.options.get("mqtt_enabled", True):
            self.task = asyncio.create_task(self._run(), name="mqtt-entities")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _births(self, client):
        async for message in client.messages:
            if str(message.topic) == "homeassistant/status" and message.payload == b"online":
                self.announced.clear()
                self.wake.set()

    async def publish(self, client):
        status = self.manager.status()
        snapshot = self.telemetry.snapshot(status["server_running"], status["certificate"]["expires"])
        for topic, config in discovery(self.telemetry.identity, snapshot).items():
            payload = json.dumps(config, separators=(",", ":"))
            if self.announced.get(topic) != payload:
                await client.publish(topic, payload, qos=1, retain=True)
                self.announced[topic] = payload
        await client.publish(f"{self.base}/state", json.dumps(snapshot), qos=1)
        for identity, state in snapshot["thermostats"].items():
            await client.publish(f"{self.base}/thermostat/{identity}/state", json.dumps(state), qos=1)
        await client.publish(f"{self.base}/availability", "online", qos=1, retain=True)

    async def _run(self):
        while True:
            try:
                settings = await broker_settings(self.manager.options)
                async with aiomqtt.Client(
                        hostname=settings["host"], port=settings["port"],
                        username=settings.get("username"), password=settings.get("password"),
                        tls_context=ssl.create_default_context() if settings.get("ssl") else None,
                        identifier=f"localstar_{self.telemetry.identity}",
                        will=aiomqtt.Will(f"{self.base}/availability", "offline", qos=1, retain=True),
                        timeout=10) as client:
                    self.announced.clear()
                    self.status = "connected"
                    print("MQTT sensor discovery connected.", flush=True)
                    await client.subscribe("homeassistant/status", qos=1)
                    births = asyncio.create_task(self._births(client))
                    try:
                        while True:
                            self.wake.clear()
                            await self.publish(client)
                            if births.done():
                                await births
                                raise RuntimeError("MQTT message stream ended")
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(self.wake.wait(), self.interval)
                    finally:
                        births.cancel()
                        with contextlib.suppress(asyncio.CancelledError, aiomqtt.MqttError):
                            await births
                        with contextlib.suppress(aiomqtt.MqttError):
                            await client.publish(f"{self.base}/availability", "offline", qos=1, retain=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Connection exceptions can embed authentication details. Never
                # print their text, broker passwords, tokens, or options.
                self.status = "waiting"
                print("MQTT sensors unavailable; retrying in 30 seconds. Skyport continues running.", flush=True)
                await asyncio.sleep(self.retry)
