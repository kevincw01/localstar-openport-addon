"""Ingress-only certificate provisioning and child-process supervision."""

import asyncio
import contextlib
import html
import json
import os
from pathlib import Path
import secrets
import time

from aiohttp import web

from addon_config import server_command, ssl_file, validate_options
from certificate_store import CertificateStore, validate_pair
from telemetry import Telemetry
from mqtt_entities import MqttEntities

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
        cert = ssl_file(self.options.get("certificate_file", "localstar-openport/server-chain.pem"), self.ssl_directory)
        key = ssl_file(self.options.get("private_key_file", "localstar-openport/server.key"), self.ssl_directory)
        _, _, root, metadata = validate_pair(cert.read_bytes(), key.read_bytes())
        return metadata | {"source": "external_ssl", "id": None, "certificate": cert,
                           "key": key, "root_pem": root}

    async def start(self):
        try:
            self.profile = await asyncio.to_thread(self.store.current)
            if self.profile is None:
                self.profile = await asyncio.to_thread(self._external_profile)
        except FileNotFoundError:
            print("No certificate pair yet. Open Web UI to generate or upload one.", flush=True)
        except (OSError, ValueError) as exc:
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
        await self._stop_process()
        try:
            await self._launch(profile)
            await asyncio.to_thread(self.store.activate, profile)
        except BaseException:
            await self._stop_process()
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

    app = web.Application(middlewares=[ingress_access], client_max_size=256 * 1024)

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

    async def root_certificate(request):
        root = await asyncio.to_thread(manager.public_root)
        if root is None:
            raise web.HTTPNotFound(text="No public root is available; upload the root CA if your chain omits it")
        return web.Response(body=root, content_type="application/x-pem-file",
                            headers={"Content-Disposition": 'attachment; filename="root-ca.pem"'})

    async def provision(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Expected JSON")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid request body")
            async with manager.lock:
                if manager.status()["has_identity"] and body.get("replace") is not True:
                    raise web.HTTPConflict(text="Confirm replacement of the active certificate identity first")
                profile = None
                try:
                    if request.match_info["action"] == "generate":
                        profile = await asyncio.to_thread(manager.store.prepare_generated)
                    else:
                        chain, key, root = (body.get(name, "") for name in ("certificate", "key", "root_ca"))
                        if not all(isinstance(value, str) for value in (chain, key, root)):
                            raise ValueError("Certificate and key must be PEM text")
                        profile = await asyncio.to_thread(manager.store.prepare_uploaded,
                                                          chain.encode(), key.encode(), root.encode() or None)
                    await manager.activate(profile)
                finally:
                    if profile is not None:
                        await asyncio.to_thread(manager.store.discard, profile)
            return web.json_response(manager.status())
        except web.HTTPException:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def startup(app):
        await manager.start()

    async def cleanup(app):
        await manager.close()

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/telemetry", telemetry)
    app.router.add_get("/root-ca.pem", root_certificate)
    app.router.add_post("/api/{action:generate|upload}", provision)
    app.router.add_static("/static", WEB_DIRECTORY)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
