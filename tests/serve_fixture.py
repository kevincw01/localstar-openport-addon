"""Run the real server against deterministic weather-provider input."""

import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

PROVIDER = {
    "current": {"temperature_2m": 68, "relative_humidity_2m": 50,
                "weather_code": 0, "wind_speed_10m": 5, "wind_direction_10m": 90, "is_day": 1},
    "daily": {"time": [f"2030-06-{day:02}" for day in range(1, 7)],
              "weather_code": [0] * 6, "temperature_2m_max": [72] * 6,
              "temperature_2m_min": [54] * 6,
              "sunrise": ["2030-06-01T06:00"] * 6, "sunset": ["2030-06-01T20:00"] * 6,
              "precipitation_probability_max": [0] * 6},
}

with patch.object(server.urllib.request, "urlopen",
                  side_effect=lambda *args, **kwargs: io.BytesIO(json.dumps(PROVIDER).encode())):
    server.main()
