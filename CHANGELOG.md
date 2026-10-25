# Changelog

## 1.0.0

- Package the shared Skyport server as a Supervisor add-on/app.
- Configure location, timezone, certificates, time sync, and legacy TLS in HA.
- Mount persistent SSL storage read-only; never generate or replace trust material at startup.
- Publish entry/session ports 9001 and 41414, with automatic boot startup.
- Log every received heartbeat and successful echo.
