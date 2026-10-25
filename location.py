"""Resolve optional overrides against HA's home location without exposing it."""

import contextlib
import json
import os
from pathlib import Path
import urllib.request
import uuid

from addon_config import validate_options

INHERIT_TIMEZONE = (None, "", "Home Assistant")


class LocationUnavailable(RuntimeError):
    """Core is not ready yet and no previously resolved location is cached."""


def fetch_home_assistant_config():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise LocationUnavailable("No Supervisor token available")
    request = urllib.request.Request("http://supervisor/core/api/config",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    return {"latitude": data.get("latitude"), "longitude": data.get("longitude"),
            "timezone": data.get("time_zone")}


def resolve_options(options, data_directory="/data", *, fetch=None):
    """Apply explicit overrides per field; persist only HA's location, never tokens."""
    validate_options(options, allow_inherit=True)
    missing = [key for key in ("latitude", "longitude") if options.get(key) is None]
    if options.get("timezone") in INHERIT_TIMEZONE:
        missing.append("timezone")
    if not missing:
        return options.copy()
    path = Path(data_directory) / "location.json"
    try:
        inherited = (fetch or fetch_home_assistant_config)()
        validate_options(inherited)
    except (OSError, ValueError, TypeError, RuntimeError):
        try:
            inherited = json.loads(path.read_text())
            validate_options(inherited)
        except (OSError, ValueError, TypeError) as exc:
            raise LocationUnavailable(
                "Waiting for Home Assistant's location. Start Core, or set latitude, longitude "
                "and an explicit timezone in Configuration.") from exc
        print("Using the cached Home Assistant location while Core is unavailable.", flush=True)
    else:
        temporary = path.with_name(uuid.uuid4().hex + ".tmp")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({key: inherited[key] for key in ("latitude", "longitude", "timezone")}, stream)
            os.replace(temporary, path)
        except OSError:
            print("Could not cache HA's location; the resolved settings are still usable.", flush=True)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
    result = options.copy()
    result.update({key: inherited[key] for key in missing})
    return validate_options(result)
