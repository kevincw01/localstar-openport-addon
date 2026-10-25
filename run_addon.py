#!/usr/bin/env python3
"""Validate Supervisor options, then replace this process with the TLS server."""

import json
import math
import os
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


def command_from_options(options, ssl_directory="/ssl"):
    """Map validated options to argv without shell expansion or shared credentials."""
    if not isinstance(options, dict):
        raise ValueError("Add-on options must be a JSON object")
    coordinates = {}
    for key, bound in (("latitude", 90), ("longitude", 180)):
        value = options.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or not -bound <= value <= bound):
            raise ValueError(f"Set {key} to a number between {-bound} and {bound}")
        coordinates[key] = value
    tz = options.get("timezone")
    if not isinstance(tz, str) or not tz:
        raise ValueError("Set timezone to an IANA name, such as Etc/UTC")
    try:
        ZoneInfo(tz)
    except (KeyError, ValueError) as exc:
        raise ValueError("Unknown IANA timezone") from exc
    for key in ("sync_time", "allow_old_tls"):
        if not isinstance(options.get(key, key == "sync_time"), bool):
            raise ValueError(f"{key} must be true or false")
    cert = ssl_file(options.get("certificate_file", "localstar-openport/server-chain.pem"), ssl_directory)
    key = ssl_file(options.get("private_key_file", "localstar-openport/server.key"), ssl_directory)
    # A mismatch or unreadable key fails before either listener starts.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    command = [sys.executable, "-u", str(Path(__file__).with_name("server.py")),
               "--bind", "0.0.0.0", "--entry-port", "9001", "--session-port", "41414",
               "--redirect-host", "connect.skyport.io",
               "--cert", str(cert), "--key", str(key),
               "--lat", str(coordinates["latitude"]), "--lon", str(coordinates["longitude"]),
               "--tz", tz]
    if not options.get("sync_time", True):
        command.append("--no-time-sync")
    if options.get("allow_old_tls", False):
        command.append("--allow-old-tls")
    return command


def main(options_path="/data/options.json"):
    try:
        with open(options_path, encoding="utf-8") as stream:
            options = json.load(stream)
        command = command_from_options(options)
    except (OSError, ValueError) as exc:
        print(f"localstar-openport configuration error: {exc}", file=sys.stderr, flush=True)
        return 1
    print("Starting localstar-openport on TCP ports 9001 and 41414", flush=True)
    os.execv(command[0], command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
