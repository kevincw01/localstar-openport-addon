"""Import HA's initial location once, then keep explicitly configured settings."""

import contextlib
import json
import os
from pathlib import Path
import urllib.request
import uuid

from addon_config import validate_options

INHERIT_TIMEZONE = (None, "", "Home Assistant")


class LocationUnavailable(RuntimeError):
    """The initial location or its persistent storage is not ready yet."""


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
    """Pin the resolved location once; explicit option edits update that snapshot."""
    validate_options(options, allow_inherit=True)
    fields = ("latitude", "longitude", "timezone")
    missing = [key for key in fields[:2] if options.get(key) is None]
    if options.get("timezone") in INHERIT_TIMEZONE:
        missing.append("timezone")
    path = Path(data_directory) / "location.json"
    saved = None
    try:
        saved = json.loads(path.read_text())
        validate_options(saved)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        if missing:
            raise LocationUnavailable(
                "Stored location cannot be read. Restore location.json from backup, or set "
                "latitude, longitude and an explicit timezone in Configuration.") from exc
        saved = None
    result = options.copy()
    if missing:
        if saved is not None:
            inherited = saved
            print("Using saved location and timezone.", flush=True)
        else:
            try:
                inherited = (fetch or fetch_home_assistant_config)()
                validate_options(inherited)
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                raise LocationUnavailable(
                    "Waiting for Home Assistant's initial location. Start Core, or set latitude, "
                    "longitude and an explicit timezone in Configuration.") from exc
        result.update({key: inherited[key] for key in missing})
    validate_options(result)
    selected = {key: result[key] for key in fields}
    if selected == saved:
        return result
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(selected, stream)
        os.replace(temporary, path)
    except OSError as exc:
        raise LocationUnavailable(
            "Could not save the configured location. Check add-on data storage; "
            "startup will retry without changing credentials.") from exc
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
    return result
