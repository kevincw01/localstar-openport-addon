# Changelog

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
