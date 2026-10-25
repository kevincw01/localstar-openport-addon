# localstar-openport add-on

This is a Supervisor add-on (called an app in newer Home Assistant versions),
installed from the Supervisor store rather than HACS. It runs in its own container,
so restarting Home Assistant Core does not stop the Skyport server. Rebooting the
HA host or restarting this add-on does interrupt it; thermostats then reconnect.
The existing Venstar integration continues to provide HVAC controls and entities.

## Install

1. In Home Assistant, open **Settings > Add-ons > Add-on Store > ⋮ > Repositories**.
   On versions using Apps terminology, open the app store and its Repositories menu.
2. Add `https://github.com/kevincw01/localstar-openport-addon`.
3. Find **localstar-openport** and select **Install**. The image is built on your
   HA machine, so the first installation can take several minutes and needs internet.
4. Start the add-on. It imports HA's home latitude, longitude, and timezone once,
   then saves them as static settings. Use optional coordinates or the timezone
   dropdown to change them explicitly.
5. Open **Web UI** and generate new credentials or upload your existing pair.
6. Enable **Start on boot** and **Watchdog** on the add-on's Info tab.

One installation serves all your thermostats. This release supports `aarch64` and
`amd64`; a plain Home Assistant Container installation has no Supervisor store.

## Certificates

The **Web UI** is served through authenticated Home Assistant ingress. Its port
8099 is internal and is not published to your network. The server may initially
show **Certificate setup required**. All three certificate-path fields default to
empty. The add-on runs the setup UI, but neither Skyport listener starts until a
valid certificate chain, matching key, and root CA are configured. Open **Web UI**
to generate or upload them.

### New installation: generate credentials

1. Choose **Generate certificate and key**. The add-on generates independent random RSA
   CA and server keys, a root CA, and a server certificate covering Skyport names.
   Both root and leaf are valid from 1970-01-01 through 2037-12-12 so a thermostat
   with an unset clock can validate them.
2. On the thermostat, open **About** and note the Firmware value (for example
   `VH6.93`). Enter that value in the add-on Web UI and download the SD-card image.
   It contains a FAT32 partition with the four-byte version stub, gallery import
   index, and active public root CA. It does not contain firmware.
3. Use Rufus or another disk-image writer to write the `.img` file to an SD card.
   In Rufus, choose **DD Image mode** if prompted; writing erases the selected card.
4. On the thermostat, open **SD Card → Import/Export**, choose **Custom Images**
   only, and complete the import. If prompted to update firmware, cancel: this
   image imports the public Skyport root CA and does not install firmware.
5. Remove the card and restart the thermostat so it reloads the root CA. Restart
   from **Menu → Settings → Installation Settings → Restart Thermostat**, or
   remove the thermostat from its base and reinstall it. Verify TLS login in the
   add-on log.

The CA's private key and the server key stay in the add-on. The web interface
does not offer private-key downloads. Newly generated credentials still require
validation on the target thermostat; software tests cannot prove on-device import.

### Existing installation: upload the working pair

1. Open **Web UI** and select your current server certificate chain (PEM),
   matching unencrypted private key (PEM), and the matching public root CA
   certificate under **Upload your working pair**.
2. The root CA must be the trust anchor already installed on the thermostats.
   If you use a new root, create an SD-card image in the Web UI and import it
   before they can trust the server. Do not upload the CA's private key.
3. Select **Upload and activate**. The add-on checks the key match, current
   certificate validity, Skyport hostname, and signatures of the certificate
   chain and root CA.
   Imported pairs may have a later notBefore than generated pairs; your existing
   working certificates are retained rather than replaced with epoch-dated ones.
4. After activation, check the displayed fingerprint and the new TLS login logs.
   Using the same working pair requires no new thermostat CA import.

Generation and upload explicitly activate a new profile and restart the Skyport
listeners. If an identity already exists, these actions replace it. A newly
generated CA must be imported to the thermostats before they can reconnect.
The add-on automatically saves the certificate, key, and root CA paths into HA's
Configuration before starting the listeners. These are file paths, never private-key contents. Refresh
the Configuration page to see them. If saving fails, Skyport remains stopped or
rolls back to the prior working setup. No user-created HA token is required to
update the add-on's own options.
Replacing it with a different CA requires importing that CA on every thermostat.
Invalid uploads leave the active identity unchanged. If the new server cannot
start, activation rolls back to the previous identity.

Managed profiles are stored below `/data/certificates` with restrictive file
permissions; `current.json` selects the active profile. They survive add-on/Core
restarts and upgrades and are included in add-on backups. Previous profiles are
retained for backup/recovery. Protect these backups because they contain private
keys, including the CA key for profiles generated by the add-on. Startup never
generates a new identity automatically. An internal monitor restarts the Skyport
child if it exits, while retaining the same selected profile.

### Existing file-based setup (still supported)

The add-on can also use explicitly configured certificate, key, and root CA
files in the read-only shared SSL directory. All three paths must be populated.

Managed setup fills paths such as `/data/certificates/<profile>/server-chain.pem`,
`/data/certificates/<profile>/server.key`, and
`/data/certificates/<profile>/root-ca.pem`. These are validated as the exact
chain/key/root files in one private managed profile. Arbitrary absolute paths and
paths escaping private storage are rejected. External SSL paths remain relative
to `/ssl`, as in the example below.

Clearing any of the three paths and restarting stops Skyport and returns to setup mode,
even when an older managed identity or files in `/ssl` still exist. Stored keys
are retained. There is no implicit default path or managed-profile fallback.
During upgrade, a verified legacy managed pair is mapped to its real paths once,
but blank settings are never filled by migration.

Using Samba or an SSH/file-management add-on with SSL directory access, create
`localstar-openport` in HA's shared **SSL** directory and copy these files there:

```text
ssl/
  localstar-openport/
    server-chain.pem
    server.key
    root-ca.pem
```

They appear inside this add-on as `/ssl/localstar-openport/server-chain.pem`,
`/ssl/localstar-openport/server.key`, and `/ssl/localstar-openport/root-ca.pem`.
The root CA must be the trust anchor installed on the thermostats. The key must remain private and readable by
the add-on. Do not copy the root CA's private key to HA; it is not needed to run
the server. Protect backups that contain the server key. When making an HA backup,
include **SSL certificates** as well as this add-on: the shared SSL folder is not
part of the add-on's private `/data` directory.

## Configuration

On initial setup, leave latitude/longitude unset and keep **Home Assistant** in
the timezone dropdown to import HA's configured home settings. The resolved
values are saved once and remain static across restarts, updates, and later
changes to HA's home location or timezone. Example for the initial import:

```yaml
timezone: Home Assistant
```

To change the saved location, set the optional fields or choose an explicit IANA
timezone, then save and restart. Each field is applied independently: unset
coordinates and **Home Assistant** reuse their saved values after initial setup,
and a coordinate of zero is valid. Example only, using London:

```yaml
latitude: 51.5074
longitude: -0.1278
timezone: Europe/London
certificate_file: localstar-openport/server-chain.pem
private_key_file: localstar-openport/server.key
root_ca_file: localstar-openport/root-ca.pem
sync_time: true
allow_old_tls: false
```

Certificate, key, and root CA fields are filled automatically by Web UI setup. If you choose
external files instead, their paths are relative to `/ssl` and must resolve inside
that directory. Only managed paths may be absolute. The add-on validates the matching
key/certificate/root CA set and location options before launching the server. The root CA
must be the public trust anchor installed on each thermostat; a new root must be imported
to the thermostats with an SD-card image before they can trust the server.

Initial location defaults are read from HA's `/api/config` using the Supervisor Core
proxy and its automatically provided token. This requires the add-on's declared
`homeassistant_api` permission; you do not need to create or paste a token.
Only the resolved latitude, longitude, and timezone are stored in the private
`/data/location.json` file. Existing valid settings from the previous release's
cache become the saved starting point. Once saved, startup does not contact HA
for these values, so Core availability cannot change them.
On first installation without saved settings, startup waits and retries every 30 seconds
until Core is available. Specifying all three fields explicitly avoids this
dependency. All explicitly configured values are saved too. Startup also waits
if storage cannot persist the selected values, to prevent a different location
being imported on the next start. Credentials are not regenerated while waiting.

To apply a later HA location change, enter the desired values in this add-on's
Configuration and restart; HA edits alone do not update the saved snapshot.
Explicit settings from earlier releases remain honored. A selected IANA zone
still follows its daylight-saving rules; the zone identifier is static, not its
UTC offset. Saved settings are included in add-on backups. If the file is damaged,
restore it from backup or set all three fields explicitly to repair it; the
add-on will not silently import a different location.

Time responses contain local wall-clock time encoded as epoch milliseconds, as the
thermostat expects. Set `sync_time: false` to disable these responses. Enable
`allow_old_tls` only for firmware that needs older TLS/cipher support. Save changes
and restart the add-on for them to take effect. Check the **Log** tab for startup,
TLS login, weather requests, and separate entries for every ping and echo.

## Home Assistant weather and diagnostic entities

The add-on publishes real HA sensor and binary-sensor entities using
[MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).
Configure the **MQTT integration** in **Settings > Devices & services**, using
the same broker the add-on uses. Discovery must be enabled (the HA default).
With the Supervisor Mosquitto broker installed, no broker credentials need to
be entered here: the add-on reads its registered service connection settings.
No HA long-lived token or custom integration is required.

The defaults are:

```yaml
mqtt_enabled: true
mqtt_host: ""
mqtt_port: 1883
mqtt_username: ""
mqtt_password: ""
mqtt_tls: false
```

Leave `mqtt_host` empty for Supervisor service discovery; the service's port,
username, password and TLS setting are then used. For an external broker, set
the host and its connection options explicitly. TLS verifies the broker against
system public roots; self-signed broker certificates are not supported in this
release. Save options and restart the add-on. Passwords and Supervisor tokens
are never included in entity state or logs. Protect add-on backups, which can
contain manually configured MQTT passwords as well as certificate keys.

If MQTT is missing or disconnected, the server and Web UI keep working and the
publisher retries every 30 seconds. Set `mqtt_enabled: false` to disable it.
The ingress page shows whether MQTT is connected, waiting, or disabled.

### Entities created

| Device | Entities |
| --- | --- |
| localstar-openport | Outdoor temperature, outdoor humidity, wind speed, compass wind direction, weather condition, today's high, today's low, today's precipitation probability |
| localstar-openport diagnostics | Skyport server running, detected thermostat count, connected thermostat count, weather last updated, cache age, fetch problem, server certificate expiry |
| Each detected thermostat | Skyport connection, last seen, last successful ping echo, last weather response |

Weather comes from the exact Open-Meteo cache used for the thermostat responses;
entities make no additional provider requests. Native temperatures are °F and
wind speed is mph; HA can convert supported units to your preferred units.
Humidity and precipitation probability are percentages. The precipitation value
is the provider's daily maximum probability, not measured rainfall.
The condition is descriptive text, not a full HA `weather` forecast entity.

Weather sensors become unavailable before the first fetch or when the cache is
30 minutes old. A failed fetch immediately turns the diagnostic problem sensor
on; an unexpired last good cache can still be shown. Existing thermostat protocol
behavior continues to serve the last cached payload. Cache age and last-update
entities show its age; the next successful update clears the problem.

Thermostats are detected only after a Skyport login, not by scanning your LAN.
Their login identifier gives a stable device ID even if the address changes.
Rename the generic devices in HA to identify rooms. Address and firmware appear
on the Skyport connection entity; the detected count also has a `thermostats`
attribute with the known clients. There is no configured room-name list.
Detected clients and the instance ID persist in private add-on data; last-seen
timestamps are checkpointed periodically and on clean shutdown. After restart,
previously detected clients remain listed but disconnected until they log in.
Overlapping sessions for the same client count as one connected thermostat.

A live Skyport session becomes disconnected on socket closure or after 180
seconds without traffic. Last seen tracks any valid post-login message, last
ping tracks a successfully sent echo, and last weather response tracks a
successfully sent weather payload. None confirms screen rendering or HVAC
operation. Keep the existing Venstar integration for indoor measurements and
thermostat controls; it is not replaced by these diagnostics.

States are published every 30 seconds, with retained discovery configurations,
broker Last Will availability, and 90-second state expiry. MQTT reconnections
and HA's standard `homeassistant/status` birth message reannounce discovery.
States themselves are not retained, avoiding old online states on restart.
Disabling MQTT leaves already discovered entities unavailable; remove those
entities/devices and their retained discovery topics if permanently retiring an
installation. Custom HA discovery/birth topic prefixes are not supported.

## Networking and migration

The default published ports are TCP **9001** (entry) and **41414** (session). Keep
the host session port at **41414**, since the entry response advertises it. Allow
the thermostat network to reach those ports on your HA host. If the thermostats
are on a separate VLAN, add narrow inter-VLAN rules for these connections and
allow DNS to your local resolver. Retain the existing HA-to-thermostat local API
access. This add-on needs outbound HTTPS and DNS for Open-Meteo; the thermostats
can remain blocked from the internet. Do not expose either listener publicly.

To migrate an existing standalone server:

1. Back up its configuration and working certificate/key pair.
2. Install this add-on, review its inherited location or overrides, start it,
   and open **Web UI**.
3. Upload the working server chain/key and confirm both listeners appear in its logs.
4. Change your firewall's Skyport destination to the HA host and change the local
   DNS override for **`connect.skyport.io`** to the HA host's LAN address.
5. Reconnect the thermostats or wait for them to reconnect. Existing sessions to
   the old server can persist until it is stopped or the devices reconnect.
6. Confirm new logins, time/weather responses, and repeated ping/echo pairs on HA.
   Check the thermostat screens too: a weather response does not prove rendering.
7. Test a thermostat reboot and a Core restart, then disable the old service after
   confirming recovery. Keep its configuration available for rollback.

You do not need to change HA's Venstar thermostat entries during this migration:
their local API addresses and entity IDs stay the same. Reusing the working
certificate/key pair avoids another CA import. If testing two servers on the
same machine, stop the old one before starting the add-on so the ports are free.

Rollback is to restore DNS/firewall routing to the old host and start the old
server. DNS changes do not move an already established session.

## Troubleshooting

- **Waiting for HA location:** start Core or specify all three location fields
  explicitly on initial setup. Saved values subsequently work independently of Core.
- **Configuration error:** check coordinate override ranges and the timezone
  selection, then open Web UI to generate or upload credentials. For the legacy file-based
  setup, check certificate/key paths below `/ssl` and that the pair matches.
- **Web UI says forbidden:** open it using HA's **Open Web UI** button. Direct
  connections to the internal management port are deliberately denied.
- **Address already in use:** another process is using a required host port. Stop
  it or use a different host. Both server listeners must start successfully.
- **Unknown CA:** the thermostat does not trust the CA for the chain being served.
  Do not generate replacement keys to fix this; import the corresponding root or
  restore the known working certificate pair.
- **Weather missing:** check for `weather sent` and verify the device's Weather
  Updates setting. After reboot, toggling Weather Updates off/on can trigger a
  fresh request. Automatic rendering recovery is not guaranteed by this server.
- **Weather fetch failed:** check the add-on host's HTTPS/DNS internet access.
  The cache is currently in memory and an initial weather fetch is needed after
  every add-on restart. Weather queries themselves do not use the HA Core API.
