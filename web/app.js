"use strict";
const byId = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="csrf-token"]').content;
let active = false;
let busy = false;

function message(text, error = false) {
  byId("notice").textContent = text;
  byId("notice").classList.toggle("error", error);
  byId("notice").hidden = false;
}

function render(status) {
  active = status.has_identity;
  const certificate = status.certificate;
  byId("server-status").textContent = status.setup_required ? "Certificate setup required" : (status.server_running ? "Server running" : (status.error ? "Needs attention" : "Skyport stopped"));
  byId("setup-notice").hidden = !status.setup_required;
  byId("server-status").classList.toggle("running", status.server_running);
  const origins = {generated: "Generated in this add-on", uploaded: "Uploaded certificate pair", external_ssl: "Using the existing pair in /ssl"};
  byId("identity-description").textContent = origins[certificate.source] || "Choose a setup method below to start the Skyport server.";
  byId("certificate-details").hidden = !certificate.fingerprint;
  byId("fingerprint").textContent = certificate.fingerprint || "";
  byId("expires").textContent = certificate.expires ? new Date(certificate.expires).toLocaleDateString() : "";
  byId("sd-card").hidden = !certificate.root_available;
  byId("server-error").hidden = !status.error;
  byId("server-error").textContent = status.error || "";
  byId("replacement").hidden = !active;
  byId("generate").textContent = active ? "Generate and replace active identity" : "Generate certificate and key";
  byId("upload-submit").textContent = active ? "Upload and activate replacement" : "Upload and activate";
  byId("mqtt-status").textContent = `MQTT: ${status.mqtt || "waiting"}`;
}

function renderTelemetry(state) {
  byId("weather-summary").textContent = state.weather_fresh
    ? `${state.temperature} °F · ${state.condition} · Humidity ${state.humidity}% · Wind ${state.wind_speed} mph ${state.wind_direction}`
    : "Weather is unavailable or older than 30 minutes. Waiting for a fresh Open-Meteo update.";
  byId("thermostat-summary").textContent = `${state.connected_thermostats} connected · ${state.detected_thermostats} detected`;
  const items = Object.entries(state.thermostats).map(([id, device]) => {
    const item = document.createElement("li");
    const lastSeen = device.last_seen ? new Date(device.last_seen).toLocaleString() : "unknown";
    item.textContent = `${device.address} · ${device.firmware} · ${device.connected ? "Connected" : "Disconnected"} · Last seen ${lastSeen}`;
    return item;
  });
  byId("thermostat-list").replaceChildren(...items);
}

async function refresh() {
  if (busy) return;
  const response = await fetch(new URL("api/status", document.baseURI), {cache: "no-store"});
  if (!response.ok) throw new Error("Unable to load status. Reopen this page through Home Assistant.");
  render(await response.json());
  const telemetry = await fetch(new URL("api/telemetry", document.baseURI), {cache: "no-store"});
  if (telemetry.ok) renderTelemetry(await telemetry.json());
}

async function provision(action, fields = {}) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  message(action === "generate" ? "Generating new credentials…" : "Validating and uploading your certificate pair…");
  try {
    const headers = {"X-CSRF-Token": token};
    let body;
    if (action === "upload") {
      body = fields;
    } else {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(fields);
    }
    const response = await fetch(new URL(`api/${action}`, document.baseURI), {
      method: "POST", headers, body
    });
    const text = await response.text();
    let status;
    try { status = JSON.parse(text); } catch (_) { status = {error: text}; }
    if (!response.ok) throw new Error(status.error || "Certificate setup failed.");
    render(status);
    byId("upload-form").reset();
    message(action === "generate" ? "Certificate, key, and root CA saved; all three Configuration paths are filled. Create an SD-card image below to import the CA on your thermostats. Refresh HA's Configuration page to see the paths." : "Your certificate, key, and root CA are active; all three Configuration paths are filled. Refresh HA's Configuration page to see them.");
  } catch (error) {
    if (action === "upload") console.error("Certificate upload failed", error.name, error.message);
    message(error.message, true);
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }
}

byId("generate").addEventListener("click", () => provision("generate"));
byId("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const cert = byId("certificate").files[0];
    const key = byId("private-key").files[0];
    const root = byId("root-ca").files[0];
    if (!cert || !key || !root) throw new Error("Select the certificate chain, matching private key, and public root CA certificate.");
    if (cert.size > 131072 || key.size > 32768 || root.size > 131072) throw new Error("Selected files exceed the PEM upload limit.");
    const files = new FormData();
    files.append("certificate", cert, cert.name);
    files.append("key", key, key.name);
    files.append("root_ca", root, root.name);
    await provision("upload", files);
  } catch (error) {
    console.error("Could not prepare certificate files for upload", error.name, error.message);
    message(error.message, true);
  }
});
byId("sd-card-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  message("Building the SD-card image…");
  try {
    const response = await fetch(new URL("api/sd-card-image", document.baseURI), {
      method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": token},
      body: JSON.stringify({firmware_version: byId("firmware-version").value})
    });
    if (!response.ok) {
      const text = await response.text();
      let detail = text;
      try { detail = JSON.parse(text).error || text; } catch (_) {}
      throw new Error(detail || "Could not create the SD-card image.");
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || "localstar-openport.img";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    message("SD-card image downloaded. Write it to the card with Rufus in DD image mode, then import Custom Images on the thermostat.");
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }
});
refresh().catch((error) => message(error.message, true));
setInterval(() => refresh().catch(() => {}), 5000);
