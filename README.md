# localstar-openport Home Assistant add-on

Run a local Skyport weather and time server for Venstar ColorTouch thermostats
as a Home Assistant Supervisor add-on/app. It runs in a separate container and
continues serving thermostats during Home Assistant Core restarts. Keep the
existing HA Venstar integration for HVAC controls and entities.

This repository contains the add-on at its root. The standalone server and
SD-card credential builder are maintained separately in
[localstar-openport](https://github.com/kevincw01/localstar-openport).

## Install

[Add this repository to Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fkevincw01%2Flocalstar-openport-addon)

1. Open **Settings > Add-ons > Add-on Store > ⋮ > Repositories** (or the Apps store
   on versions using that name).
2. Add `https://github.com/kevincw01/localstar-openport-addon`.
3. Install **localstar-openport**. The container builds locally on your HA host.
4. Start the add-on; latitude, longitude, and timezone default to Home Assistant's
   home settings. Optional coordinates and the timezone dropdown provide overrides.
   Enable **Start on boot** and **Watchdog**.
5. Open **Web UI**. For a new installation, choose **Generate and activate**, then
   download the public root CA for the SD-card tool. To migrate a server, choose
   **Upload and activate** with its existing certificate chain and matching key.
6. Point the local DNS override for `connect.skyport.io` at your HA host, and allow
   thermostat access to TCP ports **9001** and **41414** there. Keep host port 41414.

One installation serves multiple thermostats. Supports **aarch64** and **amd64**.
No HACS installation, manually created HA token, or privileged host access is required.
The add-on uses its Supervisor-provided Core API access to read HA's location;
the last valid location is cached privately for starts while Core is unavailable.
The ingress page offers two certificate workflows:

- **Generate:** create a unique epoch-valid CA and matching server certificate,
  then download only the public root for thermostat provisioning.
- **Upload:** validate and retain an existing certificate/key pair, preserving
  the identity already trusted by the thermostats. A public root is optional if
  the uploaded chain does not contain it.

Both are stored privately in persistent add-on data. Existing `/ssl` file setups
remain supported as a read-only fallback. Keys are never replaced automatically
on restart, and replacing an active identity requires explicit confirmation.
The management port is accessible only through Home Assistant ingress.

## Home Assistant entities

With HA's **MQTT integration** configured and an MQTT broker available, the add-on
automatically discovers weather sensors and Skyport diagnostics. A Supervisor
broker such as Mosquitto is detected automatically; an external broker can be
configured with the optional `mqtt_*` settings. MQTT is optional for Skyport.

- Weather: outdoor temperature, humidity, wind speed/direction, condition,
  today's high/low, and precipitation probability, all from the existing cache.
- Server: running status, detected/connected thermostat counts, weather last
  update/cache age/fetch problem, and server certificate expiry.
- Each detected thermostat: Skyport connection, last seen, last successful ping
  echo, and last weather response; its address and firmware appear as attributes.

Thermostats are discovered when they log in to this server. HA can rename the
resulting devices to match your rooms. This reports the Skyport connection;
the existing Venstar integration still supplies thermostat controls and indoor
measurements. Weather values do not prove that a thermostat rendered its screen.
The Web UI also displays weather and detected clients without MQTT.

See [DOCS.md](DOCS.md) for certificate provisioning, options, backups, migration,
rollback, and troubleshooting. If migrating an installation from the old combined
repository, add this repository separately and configure its add-on before
removing the old installation. The repository change gives it a new Supervisor ID.
Keep only one of them running on a host at a time, as they use the same ports.

## Development and tests

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
docker build -t localstar-openport:test .
python3 tests/smoke_addon.py
python3 tests/smoke_certificates.py
python3 tests/smoke_mqtt.py
python3 tests/smoke_location.py
```

The unit/TLS tests use disposable keys, synthetic weather, and local sockets.
The Docker smoke test uses temporary credentials, loopback-only host ports,
and live Open-Meteo data; `--skip-weather` skips provider access.
Use `--sudo` if your Docker daemon requires it. These tests can be run locally;
GitHub Actions test workflows are not configured. The add-on supports ARM64 and amd64.
The provisioning smoke test uses an isolated Docker network to verify the
Supervisor-only ingress gate, generation, upload, persistence, and child recovery.
The MQTT smoke test starts a disposable loopback-only Mosquitto container and
drives real TLS clients with synthetic weather through MQTT discovery/state.
The location smoke test uses a disposable Core API proxy to check inheritance,
timezone changes, and cached recovery without touching an HA installation.
Refresh the published timezone choices with `python3 tools/update_timezones.py`
after updating the development environment's IANA data.

The server implementation is maintained here for add-on releases. The standalone
project has its own copy; protocol fixes should be carried to both repositories.
No deployment configuration, pre-generated certificates, or private keys are included.
