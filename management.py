"""Ingress-only certificate provisioning and child-process supervision."""

import asyncio
import contextlib
import html
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from aiohttp import web

from addon_config import server_command, ssl_file, validate_options
from certificate_store import CertificateStore, validate_pair
from telemetry import Telemetry
from mqtt_entities import MqttEntities
from supervisor_options import save_certificate_paths
from sd_card_image import build_image, parse_firmware_version

SUPERVISOR_IP = "172.30.32.2"
WEB_DIRECTORY = Path(__file__).with_name("web")


class ServerManager:
    def __init__(self, options, store, ssl_directory="/ssl"):
        self.options = validate_options(options)
        self.store = store
        self.ssl_directory = ssl_directory
        self.profile = None
        self.process = None
        self.reader = None
        self.watchdog = None
        self.ready = set()
        self.error = None
        self.lock = asyncio.Lock()
        self.telemetry = Telemetry(store.directory.parent)
        self.mqtt = MqttEntities(self)

    def _external_profile(self):
        cert = ssl_file(self.options.get("certificate_file", ""), self.ssl_directory)
        key = ssl_file(self.options.get("private_key_file", ""), self.ssl_directory)
        root_path = ssl_file(self.options.get("root_ca_file", ""), self.ssl_directory)
        _, _, root, metadata = validate_pair(cert.read_bytes(), key.read_bytes(), root_path.read_bytes())
        return metadata | {"source": "external_ssl", "id": None, "certificate": cert,
                           "key": key, "root": root_path, "root_pem": root}

    def _paths(self, profile):
        return {'certificate_file': str(profile['certificate'].resolve()),
                'private_key_file': str(profile['key'].resolve()),
                'root_ca_file': str(profile['root'].resolve())}

    async def _set_paths(self, paths):
        await save_certificate_paths(self.store.directory.parent, paths)
        self.options.update(paths)

    def _configured_profile(self):
        cert, key, root = (self.options.get(name, '') for name in
                           ('certificate_file', 'private_key_file', 'root_ca_file'))
        absolute = [Path(path).is_absolute() for path in (cert, key, root)]
        if any(absolute):
            if not all(absolute):
                raise ValueError('Managed certificate, key, and root CA paths must all use the same storage')
            return self.store.configured(cert, key, root)
        return self._external_profile()

    async def _migrate_paths(self):
        marker = self.store.directory.parent / 'certificate-paths-v2'
        if marker.exists():
            return
        # Older releases selected a managed identity ahead of their configured
        # SSL paths. Preserve that working pair, but never fill blank settings.
        if all(self.options.get(name, '') for name in ('certificate_file', 'private_key_file')):
            previous = await asyncio.to_thread(self.store.current)
            if previous is not None:
                previous = await asyncio.to_thread(self.store.configured,
                                                  previous['certificate'], previous['key'], previous['root'])
                await self._set_paths(self._paths(previous))
        marker.touch(mode=0o600, exist_ok=True)

    async def start(self):
        self.profile = None
        try:
            await self._migrate_paths()
            if not all(self.options.get(name, '') for name in ('certificate_file', 'private_key_file', 'root_ca_file')):
                print('Certificate setup required. Skyport is stopped. Open Web UI to generate a certificate, key, and root CA or upload an existing pair.', flush=True)
            else:
                self.profile = await asyncio.to_thread(self._configured_profile)
        except FileNotFoundError:
            self.error = 'Configured certificate files are missing. Open Web UI to generate or upload a pair.'
            print(self.error, flush=True)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error = str(exc)
            print("Certificate setup requires attention; open Web UI.", flush=True)
        if self.profile is not None:
            try:
                await self._launch(self.profile)
            except (OSError, ValueError, RuntimeError) as exc:
                self.error = str(exc)
        self.watchdog = asyncio.create_task(self._watch(), name="skyport-process-watchdog")
        self.mqtt.start()

    async def _read_output(self, process):
        while line := await process.stdout.readline():
            text = line.decode("utf-8", "replace").rstrip()
            if text.startswith("LOCALSTAR_EVENT "):
                try:
                    self.telemetry.consume(json.loads(text.removeprefix("LOCALSTAR_EVENT ")))
                except (ValueError, KeyError, TypeError):
                    print("Ignored invalid child telemetry event.", flush=True)
                continue
            print(text, flush=True)
            if "entry listener on " in text:
                self.ready.add("entry")
            if "session listener on " in text:
                self.ready.add("session")

    async def _launch(self, profile):
        if not all(self.options.get(name, '') for name in ('certificate_file', 'private_key_file', 'root_ca_file')):
            raise RuntimeError('Certificate setup required. Open Web UI to generate or upload a certificate, key, and root CA.')
        command = await asyncio.to_thread(server_command, self.options,
                                          profile["certificate"], profile["key"])
        self.ready = set()
        self.process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=os.environ | {"LOCALSTAR_TELEMETRY": "1"})
        self.reader = asyncio.create_task(self._read_output(self.process), name="skyport-server-log")
        deadline = asyncio.get_running_loop().time() + 5
        while self.ready != {"entry", "session"}:
            if self.process.returncode is not None:
                code = self.process.returncode
                await self._stop_process()
                raise RuntimeError(f"Skyport server could not start (exit {code}); check the add-on log and port availability")
            if asyncio.get_running_loop().time() >= deadline:
                await self._stop_process()
                raise RuntimeError("Skyport listener startup timed out")
            await asyncio.sleep(.02)
        self.error = None

    async def _stop_process(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            if self.reader is not None:
                await self.reader
        self.reader = None
        self.ready = set()
        self.telemetry.child_stopped()

    async def _watch(self):
        while True:
            await asyncio.sleep(5)
            if time.time() - self.telemetry.last_saved >= 300:
                self.telemetry.save()
            async with self.lock:
                if self.profile is None:
                    continue
                if self.process is None or self.process.returncode is not None:
                    await self._stop_process()
                    try:
                        await self._launch(self.profile)
                    except (OSError, ValueError, RuntimeError) as exc:
                        self.error = str(exc)
                        print("Skyport restart failed; retrying in 5 seconds. " + self.error, flush=True)

    async def activate(self, profile):
        """Called under lock; roll back to the previous identity if startup fails."""
        previous = self.profile
        previous_paths = {name: self.options.get(name, '') for name in
                          ('certificate_file', 'private_key_file', 'root_ca_file')}
        await self._stop_process()
        try:
            await self._set_paths(self._paths(profile))
            (self.store.directory.parent / 'certificate-paths-v2').touch(mode=0o600, exist_ok=True)
            await self._launch(profile)
            await asyncio.to_thread(self.store.activate, profile)
        except BaseException:
            await self._stop_process()
            try:
                await self._set_paths(previous_paths)
            except (OSError, ValueError, RuntimeError):
                self.profile = None
                self.error = 'Could not restore certificate paths in HA. Skyport remains stopped; check Configuration and retry.'
                raise RuntimeError(self.error) from None
            if previous is not None:
                try:
                    await self._launch(previous)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.error = str(exc)
            raise
        self.profile = profile

    async def close(self):
        await self.mqtt.close()
        if self.watchdog is not None:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
        async with self.lock:
            await self._stop_process()
        self.telemetry.save()

    def status(self):
        running = self.process is not None and self.process.returncode is None and self.ready == {"entry", "session"}
        profile = self.profile or {}
        return {"server_running": running, "error": self.error,
                "setup_required": self.profile is None or not all(self.options.get(name, '') for name in
                                                                    ('certificate_file', 'private_key_file', 'root_ca_file')),
                "configured_paths": {name: self.options.get(name, '') for name in
                                     ('certificate_file', 'private_key_file', 'root_ca_file')},
                "has_identity": bool(self.profile) or (self.store.directory / "current.json").exists(),
                "mqtt": self.mqtt.status,
                "certificate": {key: profile.get(key) for key in
                                ("source", "fingerprint", "not_before", "expires", "root_available")}}

    def public_root(self):
        if self.profile is None or not self.profile.get("root_available"):
            return None
        if "root_pem" in self.profile:
            return self.profile["root_pem"]
        return self.profile["root"].read_bytes()


def create_application(options, data_directory="/data", ssl_directory="/ssl", *,
                       trusted_peers=(SUPERVISOR_IP,), manager=None):
    """Only Supervisor ingress can reach provisioning routes; no host UI port."""
    if manager is None:
        store = CertificateStore(Path(data_directory) / "certificates")
        manager = ServerManager(options, store, ssl_directory)
    token = secrets.token_urlsafe(32)

    @web.middleware
    async def ingress_access(request, handler):
        if request.remote not in trusted_peers:
            raise web.HTTPForbidden(text="Use Home Assistant ingress to access this page")
        if request.method != "GET":
            supplied = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied.encode(), token.encode()):
                raise web.HTTPForbidden(text="Invalid request token; reload this page")
        response = await handler(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[ingress_access], client_max_size=320 * 1024)

    async def index(request):
        text = await asyncio.to_thread((WEB_DIRECTORY / "index.html").read_text)
        base = request.headers.get("X-Ingress-Path", "").rstrip("/") + "/"
        if not base.startswith("/") or base.startswith("//"):
            base = "/"
        text = text.replace("{{CSRF_TOKEN}}", html.escape(token)).replace("{{BASE_PATH}}", html.escape(base, quote=True))
        return web.Response(text=text, content_type="text/html")

    async def status(request):
        return web.json_response(manager.status())

    async def telemetry(request):
        status = manager.status()
        return web.json_response(manager.telemetry.snapshot(status["server_running"],
                                                           status["certificate"]["expires"]))

    async def sd_card_image(request):
        if request.content_type != "application/json":
            return web.json_response({"error": "Expected a JSON request"}, status=415)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Enter the firmware version shown on the thermostat's About screen")
            version, _ = parse_firmware_version(body.get("firmware_version"))
        except (json.JSONDecodeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

        root = await asyncio.to_thread(manager.public_root)
        if root is None:
            return web.json_response({"error": "Configure a valid certificate and root CA before creating an SD-card image"}, status=409)

        with tempfile.TemporaryDirectory(prefix="localstar-sd-card-") as directory:
            path = Path(directory) / "localstar-openport.img"
            try:
                await asyncio.to_thread(build_image, path, version, root)
            except (OSError, RuntimeError) as exc:
                print(f"Could not create SD-card image: {exc}", flush=True)
                return web.json_response({"error": "Could not create the SD-card image. Check the add-on log."}, status=500)
            response = web.StreamResponse(status=200, headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(path.stat().st_size),
                "Content-Disposition": f'attachment; filename="localstar-openport-{version}.img"',
                "Cache-Control": "no-store",
            })
            await response.prepare(request)
            with path.open("rb") as image:
                while chunk := image.read(1024 * 1024):
                    await response.write(chunk)
            await response.write_eof()
            return response

    async def provision(request):
        action = request.match_info["action"]
        try:
            if action == "generate":
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType(text="Expected JSON")
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("Invalid request body")
                upload = None
            else:
                limits = {"certificate": 128 * 1024, "key": 32 * 1024, "root_ca": 128 * 1024}
                if request.content_type == "application/json":
                    # Continue accepting requests from an already-open older UI.
                    body = await request.json()
                    if not isinstance(body, dict) or not all(isinstance(body.get(name), str)
                                                             for name in limits):
                        raise ValueError("Select the certificate chain, matching private key, and public root CA")
                    upload = {name: body[name] for name in limits}
                    for name, value in upload.items():
                        if len(value.encode("utf-8")) > limits[name]:
                            raise web.HTTPRequestEntityTooLarge(max_size=limits[name],
                                                               actual_size=len(value.encode("utf-8")))
                elif request.content_type == "multipart/form-data":
                    reader = await request.multipart()
                    upload = {}
                    while part := await reader.next():
                        name = part.name
                        if name not in limits or name in upload or not part.filename:
                            raise ValueError("Choose one certificate chain, private key, and root CA file")
                        content = bytearray()
                        while chunk := await part.read_chunk(8192):
                            content.extend(chunk)
                            if len(content) > limits[name]:
                                raise web.HTTPRequestEntityTooLarge(max_size=limits[name],
                                                                   actual_size=len(content))
                        try:
                            upload[name] = bytes(content).decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise ValueError("PEM files must be UTF-8 text") from exc
                    missing = set(limits) - upload.keys()
                    if missing:
                        raise ValueError("Select the certificate chain, matching private key, and public root CA")
                else:
                    raise web.HTTPUnsupportedMediaType(text="Upload the three PEM files")
                print("Certificate upload received " + ", ".join(
                    f"{name}={len(value.encode('utf-8'))} bytes" for name, value in upload.items()), flush=True)
            async with manager.lock:
                profile = None
                try:
                    if action == "generate":
                        profile = await asyncio.to_thread(manager.store.prepare_generated)
                    else:
                        profile = await asyncio.to_thread(manager.store.prepare_uploaded,
                                                          upload["certificate"].encode(),
                                                          upload["key"].encode(),
                                                          upload["root_ca"].encode())
                        print("Certificate upload validated; activating the selected identity.", flush=True)
                    await manager.activate(profile)
                    if action == "upload":
                        print("Certificate upload activated successfully.", flush=True)
                finally:
                    if profile is not None:
                        await asyncio.to_thread(manager.store.discard, profile)
            return web.json_response(manager.status())
        except web.HTTPException as exc:
            if action == "upload":
                print(f"Certificate upload request rejected (HTTP {exc.status}).", flush=True)
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            if action == "upload":
                print(f"Certificate upload failed: {exc}", flush=True)
            return web.json_response({"error": str(exc)}, status=400)

    async def startup(app):
        await manager.start()

    async def cleanup(app):
        await manager.close()

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/telemetry", telemetry)
    app.router.add_post("/api/sd-card-image", sd_card_image)
    app.router.add_post("/api/{action:generate|upload}", provision)
    app.router.add_static("/static", WEB_DIRECTORY)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
