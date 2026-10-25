"""Supervisor option and certificate path validation shared with the UI manager."""

import math
from pathlib import Path
import ssl
import sys
from zoneinfo import ZoneInfo


def ssl_file(filename, ssl_directory):
    """Allow only files inside the read-only SSL mount, including after symlinks."""
    if not isinstance(filename, str) or not filename or Path(filename).is_absolute():
        raise ValueError("Certificate/key filenames must be relative to /ssl")
    base = Path(ssl_directory).resolve(strict=True)
    path = (base / filename).resolve(strict=True)
    if base not in path.parents or not path.is_file():
        raise ValueError("Certificate/key files must be regular files inside /ssl")
    return path


def validate_options(options, *, allow_inherit=False):
    if not isinstance(options, dict):
        raise ValueError("Add-on options must be a JSON object")
    for key, bound in (("latitude", 90), ("longitude", 180)):
        value = options.get(key)
        if allow_inherit and value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not -bound <= value <= bound):
            raise ValueError(f"Set {key} to a number between {-bound} and {bound}")
    tz = options.get("timezone")
    inherited_timezone = allow_inherit and tz in (None, "", "Home Assistant")
    if not inherited_timezone and (not isinstance(tz, str) or not tz):
        raise ValueError("Set timezone to an IANA name, such as Etc/UTC")
    try:
        if not inherited_timezone:
            ZoneInfo(tz)
    except (KeyError, ValueError) as exc:
        raise ValueError("Unknown IANA timezone") from exc
    for key in ("sync_time", "allow_old_tls", "mqtt_enabled", "mqtt_tls"):
        if not isinstance(options.get(key, key in ("sync_time", "mqtt_enabled")), bool):
            raise ValueError(f"{key} must be true or false")
    for key in ("mqtt_host", "mqtt_username", "mqtt_password"):
        if not isinstance(options.get(key, ""), str):
            raise ValueError(f"{key} must be text")
    for key in ('certificate_file', 'private_key_file'):
        if not isinstance(options.get(key, ''), str):
            raise ValueError(f'{key} must be a file path')
    port = options.get("mqtt_port", 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("mqtt_port must be a port number from 1 through 65535")
    return options


def server_command(options, cert, key):
    validate_options(options)
    # A mismatch or unreadable key fails before either listener starts.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    command = [sys.executable, "-u", str(Path(__file__).with_name("server.py")),
               "--bind", "0.0.0.0", "--entry-port", "9001", "--session-port", "41414",
               "--redirect-host", "connect.skyport.io",
               "--cert", str(cert), "--key", str(key),
               "--lat", str(options["latitude"]), "--lon", str(options["longitude"]),
               "--tz", options["timezone"]]
    if not options.get("sync_time", True):
        command.append("--no-time-sync")
    if options.get("allow_old_tls", False):
        command.append("--allow-old-tls")
    return command


def command_from_options(options, ssl_directory="/ssl"):
    validate_options(options)
    cert = ssl_file(options.get("certificate_file", ""), ssl_directory)
    key = ssl_file(options.get("private_key_file", ""), ssl_directory)
    return server_command(options, cert, key)
