"""Private runtime snapshots for weather and observed Skyport clients."""

from datetime import datetime, timezone
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

WEATHER_MAX_AGE = 1800
CLIENT_MAX_AGE = 180


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


class Telemetry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "telemetry.json"
        if self.path.exists():
            stored = json.loads(self.path.read_text())
        else:
            stored = {"id": uuid.uuid4().hex, "thermostats": {}}
        self.identity = stored["id"]
        self.thermostats = stored["thermostats"]
        self.sessions = {}
        self.weather = None
        self.weather_updated = 0
        self.weather_failed = False
        self.last_saved = 0
        self.save()

    def save(self):
        temporary = self.directory / (uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"id": self.identity, "thermostats": self.thermostats}, stream)
            os.replace(temporary, self.path)
            self.last_saved = time.time()
        except OSError:
            self.last_saved = time.time()
            print("Could not save thermostat registry; runtime tracking continues.", flush=True)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def child_stopped(self):
        self.sessions.clear()
        self.weather = None
        self.weather_updated = 0

    def consume(self, event):
        kind = event.get("event")
        now = event.get("time", time.time())
        if kind == "weather":
            self.weather = event["payload"]
            self.weather_updated = now
            self.weather_failed = False
        elif kind == "weather_error":
            self.weather_failed = True
        elif kind == "login":
            mac = re.sub(r"[:.\-]", "", str(event.get("mac", ""))).lower()
            if not re.fullmatch(r"(?:[0-9a-f]{12}|[0-9a-f]{16})", mac):
                return
            identity = hashlib.sha256(mac.encode()).hexdigest()[:16]
            new = identity not in self.thermostats
            if new and len(self.thermostats) >= 128:
                return
            device = self.thermostats.setdefault(identity, {})
            device.update(address=event["peer"], firmware=event.get("firmware", ""), last_seen=now)
            self.sessions[event["session"]] = {"id": identity, "seen": now}
            if new:
                self.save()
        elif kind == "closed":
            self.sessions.pop(event.get("session"), None)
        elif kind in ("seen", "ping", "weather_sent"):
            session = self.sessions.get(event.get("session"))
            if session is not None:
                session["seen"] = now
                device = self.thermostats[session["id"]]
                device["last_seen"] = now
                if kind == "ping":
                    device["last_ping"] = now
                elif kind == "weather_sent":
                    device["last_weather_response"] = now

    def snapshot(self, server_running, certificate_expires=None, now=None):
        now = time.time() if now is None else now
        # Deduplicate overlapping reconnects; closing an old socket must not
        # disconnect a thermostat that already has a new live session.
        self.sessions = {key: value for key, value in self.sessions.items()
                         if now - value["seen"] < CLIENT_MAX_AGE}
        connected = {session["id"] for session in self.sessions.values()} if server_running else set()
        devices = {}
        for identity, device in self.thermostats.items():
            devices[identity] = device | {"connected": identity in connected}
            for key in ("last_seen", "last_ping", "last_weather_response"):
                devices[identity][key] = timestamp(device.get(key))
        age = max(0, now - self.weather_updated) if self.weather_updated else None
        fresh = bool(server_running and self.weather and age < WEATHER_MAX_AGE)
        current = self.weather["current"] if fresh else {}
        today = self.weather["forecast"][0] if fresh else {}
        return {"server_running": server_running, "detected_thermostats": len(devices),
                "connected_thermostats": len(connected), "thermostats": devices,
                "certificate_expires": certificate_expires,
                "weather_fresh": fresh, "weather_problem": self.weather_failed or not fresh,
                "weather_updated": timestamp(self.weather_updated),
                "weather_age": round(age) if age is not None else None,
                "temperature": current.get("temp_f"), "humidity": current.get("hum"),
                "wind_speed": current.get("wind_mph"), "wind_direction": current.get("wind_dir"),
                "condition": current.get("fcttext"), "high": today.get("high_f"),
                "low": today.get("low_f"), "precipitation_probability": today.get("hum_ave")}
