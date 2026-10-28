"""Isolated OP-only M1 console; delegates identity to the unchanged M0 login."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, Fingerprint, UnixConnector, WSMsgType, web

from .stream_policy import StreamPolicyError, load_limits, validate_config

WEB = Path(__file__).resolve().parents[1] / "web"
COOKIE_NAMES = ("multicraft_browser", "multicraft_session")
PROBE_STREAM = {"width": 1280, "height": 720, "fps": 60, "bitrateKbps": 6000, "adaptive": False}


from .native_input import validate_input, validate_runtime_owner


class M1Gateway:
    def __init__(self, *, origin: str, auth_url: str, auth_authority: str,
                 certificate: Path | None, token_file: Path, media_socket: Path,
                 policy_file: Path, native_url: str = "http://127.0.0.1:18091",
                 insecure_loopback: bool = False):
        parsed = urlsplit(origin)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("an exact origin is required")
        if parsed.scheme != "https" and not (insecure_loopback and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("HTTPS is required")
        for url in (auth_url, native_url):
            endpoint = urlsplit(url)
            if endpoint.scheme not in {"http", "https"} or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"} or endpoint.username or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment:
                raise ValueError("upstream must be a loopback origin")
        if urlsplit(auth_url).scheme != "https" and not insecure_loopback:
            raise ValueError("M0 authentication requires pinned HTTPS")
        if not token_file.is_file() or token_file.is_symlink() or token_file.stat().st_mode & 0o077:
            raise ValueError("host token must be an owner-only regular file")
        self.token = token_file.read_text().strip()
        if len(self.token) < 32:
            raise ValueError("host token is too short")
        self.fingerprint = None
        if certificate:
            der = ssl.PEM_cert_to_DER_cert(certificate.read_text())
            self.fingerprint = Fingerprint(hashlib.sha256(der).digest())
        elif not insecure_loopback:
            raise ValueError("the M0 certificate pin is required")
        self.origin, self.authority = origin.rstrip("/"), parsed.netloc
        self.auth_url, self.auth_authority = auth_url.rstrip("/"), auth_authority
        self.native_url, self.media_socket, self.policy_file = native_url.rstrip("/"), media_socket, policy_file
        self.csrf: dict[str, tuple[str, float]] = {}
        self.controllers: dict[str, dict] = {}
        self.input_locks = {session: asyncio.Lock() for session in ("A", "B")}
        self.client: ClientSession | None = None
        self.media: ClientSession | None = None
        self.app = web.Application(client_max_size=32768, middlewares=[self.guard])
        self.app.add_routes([web.get("/", self.index), web.get("/health", self.health),
                             web.get("/m1-app.js", self.asset), web.get("/m1-player.js", self.asset),
                             web.get("/api/state", self.state),
                             web.post("/api/session/{session}/{action}", self.control),
                             web.get("/media/{kind}/{session}", self.proxy)])
        self.app.on_startup.append(self.startup)
        self.app.on_shutdown.append(self.shutdown)
        self.app.on_cleanup.append(self.cleanup)

    async def startup(self, _):
        validate_config(PROBE_STREAM, load_limits(self.policy_file))
        self.client = ClientSession(timeout=ClientTimeout(total=18), cookie_jar=DummyCookieJar())
        self.media = ClientSession(connector=UnixConnector(path=str(self.media_socket)), timeout=ClientTimeout(total=None))

    async def cleanup(self, _):
        for client in (self.media, self.client):
            if client:
                await client.close()

    async def shutdown(self, _):
        for lease in list(self.controllers.values()):
            if lease.get("socket"):
                await lease["socket"].close(code=1001, message=b"M1 console stopping")

    @staticmethod
    def cookie_key(request) -> str:
        values = [request.cookies.get(name, "") for name in COOKIE_NAMES]
        if not all(values) or any(len(value) > 512 for value in values):
            raise web.HTTPUnauthorized(text="请先在 M0 页面登录 asta OP")
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    async def operator(self, request) -> dict:
        self.cookie_key(request)
        headers = {"Host": self.auth_authority}
        cookies = {name: request.cookies[name] for name in COOKIE_NAMES}
        try:
            async with self.client.get(self.auth_url + "/api/me", headers=headers, cookies=cookies,
                                       ssl=self.fingerprint, allow_redirects=False,
                                       timeout=ClientTimeout(total=4)) as response:
                if response.status != 200:
                    raise web.HTTPUnauthorized(text="M0 登录已失效，请重新登录")
                account = (await response.json())["account"]
        except (ClientError, OSError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise web.HTTPServiceUnavailable(text="M0 身份服务暂不可用") from exc
        if account.get("isOp") is not True or account.get("linuxUser") != "asta":
            raise web.HTTPForbidden(text="M1 测试控制台仅允许 asta OP")
        return account

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.host != self.authority:
                raise web.HTTPForbidden(text="Host is not allowed")
            websocket = request.headers.get("Upgrade", "").lower() == "websocket"
            if websocket or request.method not in {"GET", "HEAD"}:
                if request.headers.get("Origin") != self.origin:
                    raise web.HTTPForbidden(text="Origin is not allowed")
            if request.path.startswith(("/api/", "/media/")):
                request["operator"] = await self.operator(request)
                if not request.path.startswith("/api/session/"):
                    validate_config(PROBE_STREAM, load_limits(self.policy_file))
            if request.method not in {"GET", "HEAD"}:
                key = self.cookie_key(request)
                current = self.csrf.get(key)
                supplied = request.headers.get("X-CSRF-Token", "")
                if not current or current[1] <= time.monotonic() or not secrets.compare_digest(current[0], supplied):
                    raise web.HTTPForbidden(text="CSRF 校验失败，请刷新 M1 页面")
            response = await handler(request)
        except web.HTTPException as exc:
            if request.headers.get("Upgrade", "").lower() == "websocket":
                raise
            response = web.json_response({"error": exc.text}, status=exc.status)
        except (ValueError, KeyError, StreamPolicyError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except (ClientError, OSError, asyncio.TimeoutError):
            response = web.json_response({"error": "M1 后端暂不可用"}, status=503)
        if not response.prepared:
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "no-referrer",
                                     "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'self'"})
        return response

    async def index(self, _):
        return web.FileResponse(WEB / "m1.html")

    async def asset(self, request):
        return web.FileResponse(WEB / request.path.removeprefix("/"))

    async def health(self, _):
        return web.json_response({"service": "MultiCraft", "stage": "M1-OP-console"})

    async def native(self, method: str, path: str, value=None):
        async with self.client.request(method, self.native_url + path,
                                       headers={"Authorization": "Bearer " + self.token}, json=value,
                                       allow_redirects=False) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if len(body) + len(chunk) > 256 * 1024:
                    raise web.HTTPBadGateway(text="native response too large")
                body.extend(chunk)
            return web.Response(status=response.status, body=bytes(body), content_type="application/json")

    async def state(self, request):
        now = time.monotonic()
        self.csrf = {key: value for key, value in self.csrf.items() if value[1] > now}
        key = self.cookie_key(request)
        if key not in self.csrf:
            if len(self.csrf) >= 2048:
                raise web.HTTPTooManyRequests(text="too many operator sessions")
            self.csrf[key] = (secrets.token_urlsafe(32), now + 43200)
        response = await self.native("GET", "/state")
        if response.status != 200:
            return response
        state = json.loads(response.body)
        return web.json_response({"csrf": self.csrf[key][0], "account": request["operator"],
                                  "native": state, "stream": PROBE_STREAM,
                                  "controllers": {session: lease["generation"] for session, lease in self.controllers.items()
                                                  if lease["owner"] == key and not lease["closing"]},
                                  "console": "operator-two-session-test", "audioIsolationVerified": False})

    async def control(self, request):
        session, action = request.match_info["session"], request.match_info["action"]
        if session not in {"A", "B"} or action not in {"input", "command"}:
            raise web.HTTPNotFound()
        value = await request.json()
        if action == "input":
            value = validate_input(value)
            if not all(event["type"] == "release" for event in value["events"]):
                validate_config(PROBE_STREAM, load_limits(self.policy_file))
        elif not isinstance(value, dict) or set(value) != {"type", "expectedRuntimeOwner", "expectedEpoch"} or value["type"] not in {"connect", "disconnect", "respawn", "reloadResources"}:
            raise ValueError("unsupported native command")
        else:
            validate_runtime_owner(value["expectedRuntimeOwner"])
            if type(value["expectedEpoch"]) is not int or not 0 <= value["expectedEpoch"] <= 2**53 - 1:
                raise ValueError("invalid expectedEpoch")
        async with self.input_locks[session]:
            lease = self.controllers.get(session)
            supplied = request.headers.get("X-MultiCraft-Control", "")
            if (not lease or lease["closing"] or lease["owner"] != self.cookie_key(request)
                    or not secrets.compare_digest(lease["generation"], supplied)):
                raise web.HTTPConflict(text="请先连接该会话的画面以取得控制权")
            return await self.native("POST", f"/session/{session}/{action}", value)

    async def drop_controller(self, session, lease):
        # Mark before waiting: no later HTTP request can enter this lease while
        # an already submitted native input drains through the per-session lock.
        if self.controllers.get(session) is not lease:
            return
        lease["closing"] = True
        async with self.input_locks[session]:
            if self.controllers.get(session) is not lease:
                return
            try:
                # Trusted operator cleanup deliberately has no browser epoch:
                # no new controller may acquire this slot until it completes.
                await self.native("POST", f"/session/{session}/input", {"type": "release"})
            finally:
                if self.controllers.get(session) is lease:
                    self.controllers.pop(session, None)

    async def proxy(self, request):
        session, kind = request.match_info["session"], request.match_info["kind"]
        if session not in {"A", "B"} or kind not in {"video", "audio"}:
            raise web.HTTPNotFound()
        upstream = await asyncio.wait_for(self.media.ws_connect(f"http://localhost/{kind}/{session}",
                                                               max_msg_size=4 * 1024 * 1024), 5)
        downstream = web.WebSocketResponse(max_msg_size=4096, heartbeat=5)
        lease = None
        try:
            if kind == "video":
                async with self.input_locks[session]:
                    if session in self.controllers:
                        raise web.HTTPConflict(text="该会话已有一个画面控制连接")
                    lease = {"owner": self.cookie_key(request), "generation": secrets.token_urlsafe(24),
                             "closing": False, "socket": downstream}
                    self.controllers[session] = lease
            await downstream.prepare(request)

            async def receive():
                async for message in upstream:
                    if message.type == WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)
                    elif message.type == WSMsgType.TEXT:
                        await downstream.send_str(message.data)
                    elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED}:
                        break

            async def send():
                async for message in downstream:
                    if kind != "video" or message.type != WSMsgType.TEXT:
                        await downstream.close(code=1008, message=b"unsupported client media message")
                        break
                    value = json.loads(message.data)
                    if value == {"type": "keyframe"} or (isinstance(value, dict) and set(value) == {"type", "frames"} and value["type"] == "decoded" and type(value["frames"]) is int and 0 <= value["frames"] <= 2**53):
                        await upstream.send_json(value)
                    else:
                        await downstream.close(code=1008, message=b"unsupported video feedback")
                        break

            async def authorize():
                while True:
                    await asyncio.sleep(2)
                    await self.operator(request)
                    validate_config(PROBE_STREAM, load_limits(self.policy_file))

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send()), asyncio.create_task(authorize())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await upstream.close()
            if downstream.prepared:
                await downstream.close()
            if lease is not None:
                await self.drop_controller(session, lease)
        return downstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--origin", required=True)
    parser.add_argument("--auth-url", required=True)
    parser.add_argument("--auth-authority", required=True)
    parser.add_argument("--port", type=int, default=18092)
    parser.add_argument("--owner", help="launcher generation marker for exact lifecycle ownership checks")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in {18080, 18081, 25565, 25575, 25585, 8795}:
        parser.error("choose a dedicated M1 console port")
    root = args.root.resolve()
    cert, key = root / ".runtime/tls/server.crt", root / ".runtime/tls/server.key"
    gateway = M1Gateway(origin=args.origin, auth_url=args.auth_url, auth_authority=args.auth_authority,
                        certificate=cert, token_file=root / ".runtime/m1/control.token",
                        media_socket=root / ".runtime/m1/media/control.sock", policy_file=root / ".runtime/stream-policy.json")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    web.run_app(gateway.app, host="0.0.0.0", port=args.port, ssl_context=context, access_log=None)


if __name__ == "__main__":
    main()
