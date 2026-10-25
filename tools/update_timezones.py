#!/usr/bin/env python3
"""Refresh the Supervisor timezone dropdown from Python's installed IANA data."""

from pathlib import Path
import re
from zoneinfo import available_timezones

path = Path(__file__).resolve().parents[1] / "config.yaml"
zones = sorted(zone for zone in available_timezones()
               if zone != "localtime" and not zone.startswith(("posix/", "right/")))
schema = "  timezone: list(Home Assistant|" + "|".join(zones) + ")"
text, count = re.subn(r"^  timezone: (?:str|list\(.*\))$", lambda _: schema,
                      path.read_text(), flags=re.M)
if count != 1:
    raise SystemExit("Expected exactly one timezone schema entry")
path.write_text(text)
print(f"Updated timezone dropdown: Home Assistant + {len(zones)} IANA names")
