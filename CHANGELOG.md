# Changelog

## 1.2.3

- Add a gold star with a blue nautical porthole as the Home Assistant app icon.
- Use a star-circle glyph for the ingress sidebar entry.

## 1.2.2

- Save the resolved location and timezone once; HA setting changes and restarts no longer refresh them.
- Apply explicit Configuration edits to the saved values while preserving other fields.
- Reuse the previous release's valid location cache and require successful persistence before startup.

## 1.2.1

- Inherit HA's home latitude, longitude, and timezone by default; retain optional per-field overrides.
- Replace the timezone text field with a dropdown of IANA zones and a Home Assistant default.
- Cache the last valid HA location privately for Core outages; retry first-start resolution without changing credentials.
- Read HA configuration through the Supervisor Core API using the add-on's automatically supplied token.

## 1.2.0

- Publish Open-Meteo weather and Skyport diagnostics through Home Assistant MQTT discovery.
- Discover thermostats from successful Skyport logins, with stable IDs, connection status, last seen, ping echo, and weather response timestamps.
- Report detected/connected counts, server status, weather freshness/fetch problems, and certificate expiry.
- Detect the Supervisor MQTT service automatically or accept an explicit broker; MQTT failures do not stop Skyport.
- Show weather and detected thermostats in the ingress Web UI, even without MQTT.

## 1.1.0

- Add ingress-only certificate management with unique CA/server generation and existing-pair uploads.
- Download public root certificates for SD provisioning; private keys stay in persistent add-on data.
- Validate uploaded identities and require explicit replacement; roll back if a new listener cannot start.
- Preserve the working identity across restarts/upgrades and recover a stopped Skyport child.
- Retain read-only `/ssl` file-based certificate support for existing installations.

## 1.0.0

- Package the shared Skyport server as a Supervisor add-on/app.
- Configure location, timezone, certificates, time sync, and legacy TLS in HA.
- Mount persistent SSL storage read-only; never generate or replace trust material at startup.
- Publish entry/session ports 9001 and 41414, with automatic boot startup.
- Log every received heartbeat and successful echo.
