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
4. Supply certificates and configure your location before starting it.
5. Enable **Start on boot** and **Watchdog** on the add-on's Info tab.

One installation serves all your thermostats. This release supports `aarch64` and
`amd64`; a plain Home Assistant Container installation has no Supervisor store.

## Certificates

Use the same server chain and key that match the CA already imported on the
thermostats. For a new installation, generate them using the
[standalone project's SD-card builder](https://github.com/kevincw01/localstar-openport#build-the-sd-card-and-generate-credentials)
and import its root on the devices first. The add-on deliberately never
generates or rotates certificates automatically.

Using Samba or an SSH/file-management add-on with SSL directory access, create
`localstar-openport` in HA's shared **SSL** directory and copy these files there:

```text
ssl/
  localstar-openport/
    server-chain.pem
    server.key
```

They appear inside this add-on as `/ssl/localstar-openport/server-chain.pem` and
`/ssl/localstar-openport/server.key`. The key must remain private and readable by
the add-on. Do not copy the root CA's private key to HA; it is not needed to run
the server. Protect backups that contain the server key. When making an HA backup,
include **SSL certificates** as well as this add-on: the shared SSL folder is not
part of the add-on's private `/data` directory.

## Configuration

Latitude, longitude, and timezone have no installation defaults: fill them in
before starting. Example only, using London:

```yaml
latitude: 51.5074
longitude: -0.1278
timezone: Europe/London
certificate_file: localstar-openport/server-chain.pem
private_key_file: localstar-openport/server.key
sync_time: true
allow_old_tls: false
```

Certificate/key paths are relative to `/ssl` and must resolve inside that directory.
Absolute paths and escaping symlinks are rejected. The add-on validates the matching
key/certificate pair and location options before launching the server.

Time responses contain local wall-clock time encoded as epoch milliseconds, as the
thermostat expects. Set `sync_time: false` to disable these responses. Enable
`allow_old_tls` only for firmware that needs older TLS/cipher support. Save changes
and restart the add-on for them to take effect. Check the **Log** tab for startup,
TLS login, weather requests, and separate entries for every ping and echo.

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
2. Install this add-on, copy that pair to HA's SSL directory, and configure location.
3. Start it and confirm both listeners appear in its logs.
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

- **Configuration error:** set all three location fields, check timezone spelling,
  and check that certificate/key paths exist below `/ssl` and match each other.
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
  every add-on restart. No HA Core API or token is required.
