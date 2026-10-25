#!/usr/bin/env python3
"""Local Skyport stand-in for Venstar ColorTouch thermostats.

Listens on two TLS ports:
  9001  entry point; sends the plain-text redirect "<host>:<port>"
  41414 session; length-prefixed JSON command protocol

Serves LOCALWEATHER (59) from Open-Meteo.
"""

import argparse
import json
import os
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request
import warnings
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CMD_PING = 1
CMD_LOGIN = 2
CMD_LOGINACK = 3
CMD_NOTIFICATIONSETUP = 13
CMD_NOTIFICATIONRESPONSE = 14
CMD_DATETIMESETUP = 45
CMD_DATETIMERESPONSE = 46
CMD_LOCALWEATHER = 59
CMD_LOCALWEATHERRESPONSE = 60
CMD_OBSERVEALERT = 78

# The card builder generates these files outside the SD-card tree.
# Always serve the certificate matching the root imported on the thermostat.
DEFAULT_CERT = "credentials/server-chain.pem"
DEFAULT_KEY = "credentials/server.key"

WMO = {
    0: ("sunny", "Clear"),
    1: ("sunny-very-few-clouds", "Mostly Clear"),
    2: ("few-clouds", "Partly Cloudy"),
    3: ("overcast", "Cloudy"),
    45: ("fog", "Fog"),
    48: ("fog", "Freezing Fog"),
    51: ("showers-scattered", "Light Drizzle"),
    53: ("showers-scattered", "Drizzle"),
    55: ("showers", "Heavy Drizzle"),
    56: ("sleet", "Freezing Drizzle"),
    57: ("sleet", "Freezing Drizzle"),
    61: ("showers-scattered", "Light Rain"),
    63: ("showers", "Rain"),
    65: ("showers", "Heavy Rain"),
    66: ("sleet", "Freezing Rain"),
    67: ("sleet", "Freezing Rain"),
    71: ("snow", "Light Snow"),
    73: ("snow", "Snow"),
    75: ("snow", "Heavy Snow"),
    77: ("snow", "Snow Grains"),
    80: ("showers-scattered", "Showers"),
    81: ("showers", "Showers"),
    82: ("thundershower", "Heavy Showers"),
    85: ("snow", "Snow Showers"),
    86: ("snow", "Heavy Snow Showers"),
    95: ("thundershower", "Thunderstorm"),
    96: ("thundershower", "Thunderstorm"),
    99: ("thundershower", "Thunderstorm"),
}

WIND_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

log_lock = threading.Lock()


def log(msg):
    with log_lock:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def emit_event(kind, **fields):
    """Private child-to-manager events; opt-in so standalone output stays compatible."""
    if os.environ.get("LOCALSTAR_TELEMETRY") == "1":
        with log_lock:
            print("LOCALSTAR_EVENT " + json.dumps({"event": kind, "time": time.time(), **fields},
                                                 separators=(",", ":")), flush=True)


def f_to_c(f):
    return (f - 32) * 5.0 / 9.0


def wind_dir_name(deg):
    try:
        return WIND_DIRS[int((float(deg) % 360) / 22.5 + 0.5) % 16]
    except (TypeError, ValueError):
        return "N"


class WeatherCache:
    def __init__(self, lat, lon, tz, refresh=900):
        self.lat = lat
        self.lon = lon
        self.tz = tz
        self.refresh = refresh
        self.data = None
        self.updated = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def payload(self):
        with self.lock:
            return self.data

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._fetch()
            except Exception as exc:  # noqa: BLE001
                log(f"weather fetch failed: {exc}")
                emit_event("weather_error")
            self.stop.wait(self.refresh)

    def _fetch(self):
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.lat}&longitude={self.lon}"
            "&current=temperature_2m,relative_humidity_2m,weather_code,"
            "wind_speed_10m,wind_direction_10m,is_day"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
            "sunrise,sunset,precipitation_probability_max"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&timezone={self.tz}&forecast_days=6"
        )
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = json.loads(resp.read().decode())

        cur = raw["current"]
        day = raw["daily"]
        code = int(cur.get("weather_code", 0))
        icon, condition = WMO.get(code, ("few-clouds", "Cloudy"))
        if code in (0, 1) and not cur.get("is_day", 1):
            icon, condition = "sunny-very-few-clouds", "Clear"

        temp_f = float(cur["temperature_2m"])
        forecast = []
        for i, date in enumerate(day["time"]):
            dcode = int(day["weather_code"][i])
            dicon, dcond = WMO.get(dcode, ("few-clouds", "Cloudy"))
            high_f = float(day["temperature_2m_max"][i])
            low_f = float(day["temperature_2m_min"][i])
            entry = {
                "weekday": datetime.strptime(date, "%Y-%m-%d").strftime("%a"),
                "high_f": round(high_f),
                "low_f": round(low_f),
                "high_c": round(f_to_c(high_f)),
                "low_c": round(f_to_c(low_f)),
                "icon": dicon,
                "condition": dcond,
            }
            if day.get("precipitation_probability_max"):
                entry["hum_ave"] = int(day["precipitation_probability_max"][i] or 0)
            forecast.append(entry)

        def sun(key):
            stamp = datetime.fromisoformat(day[key][0])
            return {"hour": stamp.hour, "minute": stamp.minute}

        payload = {
            "current": {
                "temp_f": round(temp_f),
                "temp_c": round(f_to_c(temp_f)),
                "icon": icon,
                "wind_mph": round(float(cur.get("wind_speed_10m") or 0)),
                "wind_dir": wind_dir_name(cur.get("wind_direction_10m")),
                "hum": round(float(cur.get("relative_humidity_2m") or 0)),
                "fcttext": condition,
                "fcttext_metric": condition,
            },
            "forecast": forecast,
            "sun_phase": {"sunrise": sun("sunrise"), "sunset": sun("sunset")},
            "location": "Local",
        }
        with self.lock:
            self.data = payload
            self.updated = time.time()
        emit_event("weather", payload=payload)
        log(f"weather updated: {payload['current']['temp_f']}F {condition}")


def recv_exact(conn, size):
    buf = b""
    while len(buf) < size:
        chunk = conn.recv(size - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def send_json(conn, obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    conn.sendall(struct.pack(">I", len(body)) + body)


def local_wall_ms(tz):
    """Encode the zone's wall-clock time as epoch ms.

    The thermostat keeps a local clock, not UTC: startDST/stopDST in stat.mxe
    simply add/subtract 3600 s via udate, and nothing converts for display. So
    Skyport sends local time encoded as if it were UTC, and so must we.
    """
    local = datetime.now(ZoneInfo(tz))
    return int(local.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _cert_utc(cert, field):
    """not_valid_before_utc() is cryptography>=42; older builds are naive UTC."""
    value = getattr(cert, field + "_utc", None)
    if value is not None:
        return value
    return getattr(cert, field).replace(tzinfo=timezone.utc)


def describe_cert(cert):
    """Summarize the leaf so the log shows which anchor generation is live."""
    try:
        from cryptography import x509
    except ImportError:
        return None
    try:
        with open(cert, "rb") as fh:
            leaf = x509.load_pem_x509_certificate(fh.read())
    except (OSError, ValueError) as exc:
        return f"unreadable ({exc})"
    not_before = _cert_utc(leaf, "not_valid_before")
    not_after = _cert_utc(leaf, "not_valid_after")
    cn = leaf.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    return (f"CN={cn[0].value if cn else '?'} "
            f"{not_before:%Y-%m-%d}..{not_after:%Y-%m-%d}")


def make_context(cert, key, allow_old_tls):
    for path, what in ((cert, "certificate"), (key, "private key")):
        if not os.path.isfile(path):
            raise SystemExit(f"{what} not found: {path}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(cert, key)
    except ssl.SSLError as exc:
        raise SystemExit(f"cannot load {cert} / {key}: {exc}") from exc
    ctx.verify_mode = ssl.CERT_NONE

    summary = describe_cert(cert)
    log(f"serving {cert}" + (f" ({summary})" if summary else ""))
    if summary and "1970-01-01" not in summary:
        log("note: leaf is not 1970-dated, so a thermostat that boots with an "
            "unset clock rejects it; import a root valid at epoch zero")

    if allow_old_tls:
        try:
            # VH-6.93 offers TLSv1-era ciphers; the deprecation notice is
            # expected here and only adds noise to the journal.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except (ssl.SSLError, ValueError) as exc:
            log(f"could not lower TLS minimum: {exc}")
    return ctx


def create_listener(bind, port):
    """Bind synchronously so either port failing aborts the whole server startup."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, port))
        srv.listen(8)
    except OSError:
        srv.close()
        raise
    return srv


def entry_server(ctx, bind, port, redirect, listener=None):
    srv = listener if listener is not None else create_listener(bind, port)
    log(f"entry listener on {bind}:{port} -> redirect {redirect}")
    while True:
        raw, addr = srv.accept()
        threading.Thread(target=entry_client, args=(ctx, raw, addr, redirect), daemon=True).start()


def entry_client(ctx, raw, addr, redirect):
    try:
        raw.settimeout(15)
        conn = ctx.wrap_socket(raw, server_side=True)
        log(f"entry TLS ok from {addr[0]}")
        conn.sendall(redirect.encode())
        time.sleep(0.5)
        conn.close()
        log(f"entry redirect sent to {addr[0]}")
    except Exception as exc:  # noqa: BLE001
        log(f"entry error from {addr[0]}: {exc}")
        try:
            raw.close()
        except OSError:
            pass


def session_server(ctx, bind, port, weather, time_sync, listener=None):
    srv = listener if listener is not None else create_listener(bind, port)
    log(f"session listener on {bind}:{port}")
    while True:
        raw, addr = srv.accept()
        threading.Thread(target=session_client, args=(ctx, raw, addr, weather, time_sync), daemon=True).start()


def session_client(ctx, raw, addr, weather, time_sync):
    peer = addr[0]
    pings = [0]
    session_id = uuid.uuid4().hex
    identified = False
    try:
        raw.settimeout(180)
        conn = ctx.wrap_socket(raw, server_side=True)
        log(f"session TLS ok from {peer} ({conn.version()})")
    except Exception as exc:  # noqa: BLE001
        log(f"session TLS error from {peer}: {exc}")
        try:
            raw.close()
        except OSError:
            pass
        return

    try:
        while True:
            header = recv_exact(conn, 4)
            if header is None:
                break
            length = struct.unpack(">I", header)[0]
            if length <= 0 or length > 65536:
                log(f"bad frame length {length} from {peer}")
                break
            body = recv_exact(conn, length)
            if body is None:
                break
            try:
                msg = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                log(f"non-JSON frame from {peer}: {body[:80]!r}")
                continue
            if not isinstance(msg, dict):
                continue
            if identified:
                emit_event("seen", session=session_id)
            cmd = msg.get("command")
            if cmd == CMD_LOGIN:
                log(f"LOGIN from {peer}: mac={msg.get('mac')} fw={msg.get('fwVer')}")
                send_json(conn, {"command": CMD_LOGINACK})
                if not identified:
                    emit_event("login", session=session_id, peer=peer, mac=msg.get("mac"),
                               firmware=str(msg.get("fwVer", ""))[:80])
                    identified = True
            elif cmd == CMD_PING:
                # The stat sends PING every 60s as its heartbeat (Timer(60) ->
                # keepAlive). Echoing it back matters: inbound PING is the only
                # thing that clears SkyportEngine's dead-socket flag `r`, and
                # reconCheck (Timer(210)) tears the socket down when `r` is still
                # set 210s later. The firmware's own dispatcher skips PING
                # (`a.command!==E.PING`), so a bare echo is all it wants; normal
                # traffic such as LOGINACK does not count as liveness.
                pings[0] += 1
                sequence = msg.get("sequence")
                log(f"PING received from {peer} (#{pings[0]}, sequence={sequence})")
                send_json(conn, {"command": CMD_PING, "sequence": sequence})
                log(f"PING echo sent to {peer} (#{pings[0]}, sequence={sequence})")
                if identified:
                    emit_event("ping", session=session_id)
                continue
            elif cmd == CMD_OBSERVEALERT:
                # alert subscription push; no reply expected
                log(f"observe-alert subscription from {peer}")
            elif cmd == CMD_NOTIFICATIONSETUP:
                send_json(conn, {"command": CMD_NOTIFICATIONRESPONSE, "observe": []})
            elif cmd == CMD_DATETIMESETUP:
                if time_sync:
                    # Firmware does new Date(a.timestamp), which takes MILLISECONDS,
                    # then calls udate with getTime()/1e3. Seconds here set the
                    # clock to 1970 and every later TLS handshake fails. The
                    # device stores local time, so send local, not UTC.
                    stamp = local_wall_ms(weather.tz)
                    log(f"time sent to {peer}: "
                        f"{datetime.fromtimestamp(stamp / 1000, timezone.utc):%Y-%m-%d %H:%M:%S} {weather.tz}")
                    send_json(conn, {"command": CMD_DATETIMERESPONSE,
                                     "timestamp": stamp})
            elif cmd == CMD_LOCALWEATHER:
                data = weather.payload()
                if data is None:
                    send_json(conn, {"command": CMD_LOCALWEATHERRESPONSE,
                                     "sequence": msg.get("sequence"),
                                     "error": True,
                                     "data": "{\"error\":true}"})
                else:
                    send_json(conn, {"command": CMD_LOCALWEATHERRESPONSE,
                                     "sequence": msg.get("sequence"),
                                     "location": data["location"],
                                     "data": json.dumps(data, separators=(",", ":"))})
                    log(f"weather sent to {peer}")
                    if identified:
                        emit_event("weather_sent", session=session_id)
            else:
                log(f"unhandled command {cmd} from {peer}")
    except Exception as exc:  # noqa: BLE001
        log(f"session error from {peer}: {exc}")
    finally:
        try:
            conn.close()
        except OSError:
            pass
        if identified:
            emit_event("closed", session=session_id)
        log(f"session closed {peer}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--entry-port", type=int, default=9001)
    ap.add_argument("--session-port", type=int, default=41414)
    ap.add_argument("--redirect-host", default="connect.skyport.io")
    ap.add_argument("--cert", default=DEFAULT_CERT)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--tz", required=True, help="IANA timezone for thermostat wall-clock time")
    ap.add_argument("--no-time-sync", action="store_true")
    ap.add_argument("--allow-old-tls", action="store_true",
                    help="Explicitly permit legacy TLS versions and ciphers")
    args = ap.parse_args()

    if not -90 <= args.lat <= 90 or not -180 <= args.lon <= 180:
        ap.error("latitude must be -90..90 and longitude -180..180")
    try:
        ZoneInfo(args.tz)
    except (KeyError, ValueError):
        ap.error("unknown timezone; install tzdata if timezone data is unavailable")

    redirect = f"{args.redirect_host}:{args.session_port}"
    ctx = make_context(args.cert, args.key, args.allow_old_tls)

    def shutdown(_signum, _frame):
        log("shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Bind both ports before starting workers or fetching weather. A partial
    # startup must exit so Supervisor can report the failure and recover it.
    try:
        with create_listener(args.bind, args.entry_port) as entry_listener, \
                create_listener(args.bind, args.session_port) as session_listener:
            weather = WeatherCache(args.lat, args.lon, args.tz)
            weather.start()
            try:
                threading.Thread(
                    target=session_server,
                    args=(ctx, args.bind, args.session_port, weather, not args.no_time_sync,
                          session_listener), daemon=True,
                ).start()
                entry_server(ctx, args.bind, args.entry_port, redirect, entry_listener)
            finally:
                weather.stop.set()
    except OSError as exc:
        raise SystemExit(f"Cannot run Skyport listeners: {exc}") from exc


if __name__ == "__main__":
    main()
