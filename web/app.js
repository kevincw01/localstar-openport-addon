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
  byId("server-status").textContent = status.server_running ? "Server running" : (status.error ? "Needs attention" : (status.has_identity ? "Starting server" : "Waiting for credentials"));
  byId("server-status").classList.toggle("running", status.server_running);
  const origins = {generated: "Generated in this add-on", uploaded: "Uploaded certificate pair", external_ssl: "Using the existing pair in /ssl"};
  byId("identity-description").textContent = origins[certificate.source] || "Choose a setup method below to start the Skyport server.";
  byId("certificate-details").hidden = !certificate.fingerprint;
  byId("fingerprint").textContent = certificate.fingerprint || "";
  byId("expires").textContent = certificate.expires ? new Date(certificate.expires).toLocaleDateString() : "";
  byId("download-root").hidden = !certificate.root_available;
  byId("root-note").hidden = !certificate.root_available;
  byId("server-error").hidden = !status.error;
  byId("server-error").textContent = status.error || "";
  byId("replacement").hidden = !active;
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
  if (active && !byId("confirm-replace").checked) {
    message("Confirm replacement of the active certificate pair before continuing.", true);
    return;
  }
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  message(action === "generate" ? "Generating new credentials…" : "Validating and uploading your certificate pair…");
  try {
    const response = await fetch(new URL(`api/${action}`, document.baseURI), {
      method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": token},
      body: JSON.stringify({...fields, replace: byId("confirm-replace").checked})
    });
    const text = await response.text();
    let status;
    try { status = JSON.parse(text); } catch (_) { status = {error: text}; }
    if (!response.ok) throw new Error(status.error || "Certificate setup failed.");
    render(status);
    byId("confirm-replace").checked = false;
    byId("upload-form").reset();
    message(action === "generate" ? "New credentials are active. Download the public root CA and import it on your thermostats." : "Your certificate pair is active. The server is ready for thermostat connections.");
  } catch (error) {
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
    if (!cert || !key) throw new Error("Select a certificate chain and matching private key.");
    if (cert.size > 131072 || key.size > 32768 || (root && root.size > 131072)) throw new Error("Selected files exceed the PEM upload limit.");
    await provision("upload", {certificate: await cert.text(), key: await key.text(), root_ca: root ? await root.text() : ""});
  } catch (error) { message(error.message, true); }
});
refresh().catch((error) => message(error.message, true));
setInterval(() => refresh().catch(() => {}), 5000);
