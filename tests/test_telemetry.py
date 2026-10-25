import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telemetry import Telemetry
from mqtt_entities import discovery, broker_settings, MqttEntities


def weather():
    return {"current": {"temp_f": 68, "hum": 50, "wind_mph": 5, "wind_dir": "E", "fcttext": "Clear"},
            "forecast": [{"high_f": 72, "low_f": 54, "hum_ave": 15}]}


class TestTelemetry(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.telemetry = Telemetry(self.temporary.name)

    def login(self, session, now=1000):
        self.telemetry.consume({"event": "login", "session": session, "time": now,
                                "mac": "02:00:00:00:00:01", "peer": "127.0.0.1", "firmware": "VH6.93"})

    def test_reconnect_is_deduplicated_and_old_close_does_not_disconnect_new_session(self):
        self.login("old")
        self.login("new", 1010)
        self.telemetry.consume({"event": "closed", "session": "old"})
        state = self.telemetry.snapshot(True, now=1020)
        self.assertEqual(state["detected_thermostats"], 1)
        self.assertEqual(state["connected_thermostats"], 1)
        self.assertEqual(self.telemetry.snapshot(True, now=1191)["connected_thermostats"], 0)

    def test_seen_ping_and_weather_response_are_distinct_and_persist_without_false_reconnection(self):
        self.login("one")
        for event, stamp in (("seen", 1010), ("ping", 1020), ("weather_sent", 1030)):
            self.telemetry.consume({"event": event, "session": "one", "time": stamp})
        self.telemetry.save()
        restored = Telemetry(self.temporary.name)
        self.assertEqual(restored.identity, self.telemetry.identity)
        state = restored.snapshot(True, now=1040)
        self.assertEqual(state["connected_thermostats"], 0)
        device = next(iter(state["thermostats"].values()))
        self.assertEqual(device["last_seen"], "1970-01-01T00:17:10+00:00")
        self.assertEqual(device["last_ping"], "1970-01-01T00:17:00+00:00")
        self.assertEqual(device["last_weather_response"], device["last_seen"])
        self.assertEqual((Path(self.temporary.name) / "telemetry.json").stat().st_mode & 0o777, 0o600)

    def test_weather_error_and_staleness_are_visible_without_extending_cache_lifetime(self):
        self.telemetry.consume({"event": "weather", "time": 1000, "payload": weather()})
        fresh = self.telemetry.snapshot(True, now=1100)
        self.assertTrue(fresh["weather_fresh"])
        self.assertEqual(fresh["temperature"], 68)
        self.assertEqual(fresh["precipitation_probability"], 15)
        self.telemetry.consume({"event": "weather_error"})
        self.assertTrue(self.telemetry.snapshot(True, now=1100)["weather_problem"])
        stale = self.telemetry.snapshot(True, now=2801)
        self.assertFalse(stale["weather_fresh"])
        self.assertIsNone(stale["temperature"])
        self.assertEqual(stale["weather_age"], 1801)
        self.telemetry.child_stopped()
        self.assertIsNone(self.telemetry.snapshot(True, now=2802)["weather_updated"])

    def test_only_valid_identifiers_create_devices_and_unidentified_pings_do_not(self):
        self.telemetry.consume({"event": "login", "session": "bad", "mac": "invalid", "peer": "127.0.0.1"})
        self.telemetry.consume({"event": "ping", "session": "unknown"})
        self.assertEqual(self.telemetry.snapshot(True)["detected_thermostats"], 0)

    def test_registry_write_failure_does_not_interrupt_runtime_tracking(self):
        previous = self.telemetry.path.read_bytes()
        with patch("telemetry.os.replace", side_effect=OSError("disk full")), patch("builtins.print"):
            self.login("one")
        self.assertEqual(self.telemetry.snapshot(True, now=1001)["connected_thermostats"], 1)
        self.assertEqual(self.telemetry.path.read_bytes(), previous)
        self.assertFalse(list(self.telemetry.directory.glob('*.tmp')))

    def test_discovery_has_stable_ids_correct_units_and_availability(self):
        self.login("one")
        state = self.telemetry.snapshot(True, now=1000)
        configs = discovery(self.telemetry.identity, state)
        temp = next(value for value in configs.values() if value["name"] == "Outdoor temperature")
        self.assertEqual(temp["unit_of_measurement"], "°F")
        self.assertEqual(temp["state_class"], "measurement")
        self.assertEqual(temp["availability_mode"], "all")
        self.assertEqual(temp["expire_after"], 90)
        client = next(value for value in configs.values() if value["name"] == "Skyport connection")
        self.assertEqual(client["device_class"], "connectivity")
        self.assertEqual(client["entity_category"], "diagnostic")
        self.assertEqual(client["payload_on"], "ON")
        self.assertEqual(configs, discovery(Telemetry(self.temporary.name).identity, state))
        self.assertNotIn("PRIVATE KEY", json.dumps(configs))


class TestMqtt(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_service_uses_automatic_auth_and_broker_settings(self):
        response = MagicMock()
        response.json = AsyncMock(return_value={"result": "ok", "data": {
            "host": "core-mosquitto", "port": 1883, "username": "generated", "password": "disposable"}})
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        client = MagicMock()
        client.return_value.__aenter__ = AsyncMock(return_value=session)
        with patch("mqtt_entities.ClientSession", client), \
                patch.dict("os.environ", {"SUPERVISOR_TOKEN": "disposable-supervisor-token"}):
            result = await broker_settings({})
        self.assertEqual(result["host"], "core-mosquitto")
        session.get.assert_called_once_with("http://supervisor/services/mqtt",
            headers={"Authorization": "Bearer disposable-supervisor-token"})

    async def test_explicit_broker_settings_skip_supervisor(self):
        result = await broker_settings({"mqtt_host": "localhost", "mqtt_port": 1884,
                                        "mqtt_username": "test", "mqtt_password": "disposable", "mqtt_tls": True})
        self.assertEqual(result["host"], "localhost")
        self.assertEqual(result["password"], "disposable")
        self.assertTrue(result["ssl"])

    async def test_broker_outage_does_not_stop_skyport_and_does_not_log_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = type("Manager", (), {"options": {"mqtt_enabled": True},
                                          "telemetry": Telemetry(directory)})()
            publisher = MqttEntities(manager, retry=.01)
            with patch("mqtt_entities.broker_settings", AsyncMock(side_effect=RuntimeError("PRIVATE-secret"))), \
                    patch("builtins.print") as output:
                publisher.start()
                await asyncio.sleep(.03)
                await publisher.close()
                self.assertEqual(publisher.status, "waiting")
                self.assertGreater(output.call_count, 0)
                self.assertNotIn("PRIVATE-secret", str(output.call_args_list))


if __name__ == "__main__":
    unittest.main()
