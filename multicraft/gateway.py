"""M0 authenticated same-origin gateway for a single isolated Selkies session."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import secrets
import time
import zlib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .auth import AuthError, AuthManager
from .stream_policy import DEFAULT_CONFIG, ABSOLUTE_MAXIMUMS, StreamPolicyError, clamp_config, load_policy, load_limits, validate_config, validate_limits

BROWSER_COOKIE = "multicraft_browser"
SESSION_COOKIE = "multicraft_session"
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle)
    os.replace(temporary, path)


def read_json(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Gateway:
    def __init__(self, *, backend: str, master_token_file: Path, accounts_file: Path,
                 origin: str, stream_request: Path, stream_effective: Path,
                 runtime_state: Path | None = None, minecraft_username: str = "M0Player",
                 insecure_loopback: bool = False, apply_timeout: float = 15,
                 test_egress_file: Path | None = None, policy_file: Path | None = None):
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("origin must be an exact HTTP(S) origin")
        if parsed.scheme != "https" and not (insecure_loopback and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("HTTP is permitted only for an explicit loopback test")
        self.origin = origin.rstrip("/")
        self.authority = parsed.netloc
        self.secure = parsed.scheme == "https"
        self.backend = backend.rstrip("/")
        backend_url = urlsplit(self.backend)
        if backend_url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("M0 backend must be loopback")
        self.backend_origin = f"{backend_url.scheme}://{backend_url.netloc}"
        self.master_token = master_token_file.read_text().strip()
        if len(self.master_token) < 24 or master_token_file.stat().st_mode & 0o077:
            raise ValueError("master token must be strong and stored with mode 0600")
        self.auth = AuthManager(accounts_file, self.origin)
        self.request_file, self.effective_file = Path(stream_request), Path(stream_effective)
        self.state_file = runtime_state
        self.policy_file = Path(policy_file) if policy_file else self.request_file.parent / "stream-policy.json"
        load_policy(self.policy_file)
        self.minecraft_username = minecraft_username
        self.apply_timeout = apply_timeout
        self.apply_lock = asyncio.Lock()
        self.lease_lock = asyncio.Lock()
        self.verify_slots = asyncio.Semaphore(4)
        self.generation = int(read_json(self.effective_file).get("generation", 0))
        self.lease: dict | None = None
        self.login_rates: dict[str, tuple[float, int]] = {}
        self.test_egress_file = test_egress_file
        self.client: ClientSession | None = None
        self.watchdog: asyncio.Task | None = None
        self.app = web.Application(client_max_size=32768, middlewares=[self.guard])
        self.app.add_routes([
            web.get("/", self.index), web.get("/app.js", self.asset), web.get("/style.css", self.asset),
            web.get("/game-input.js", self.asset),
            web.get("/health", self.health), web.get("/api/bootstrap", self.bootstrap),
            web.post("/api/auth/challenge", self.challenge), web.post("/api/auth/verify", self.verify),
            web.post("/api/auth/select", self.select), web.get("/api/me", self.me),
            web.post("/api/logout", self.logout), web.get("/api/state", self.state),
            web.post("/api/lease", self.acquire), web.delete("/api/lease", self.release),
            web.post("/api/stream-config", self.stream_config),
            web.get("/api/op/stream-limits", self.get_limits),
            web.put("/api/op/stream-limits", self.set_limits),
            web.route("*", "/stream/{tail:.*}", self.proxy),
        ])
        self.app.on_startup.append(self.startup)
        self.app.on_cleanup.append(self.cleanup)

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.host != self.authority:
                raise web.HTTPForbidden(text="Host is not allowed")
            if request.path == "/api/bootstrap" or request.path.startswith("/api/auth/"):
                now = time.monotonic()
                self.login_rates = {key: value for key, value in self.login_rates.items() if value[0] > now}
                # Count the actual TCP peer, never an untrusted forwarded header.
                key = request.remote or "unknown"
                expires, used = self.login_rates.get(key, (now + 60, 0))
                if used >= 30 or (key not in self.login_rates and len(self.login_rates) >= 2048):
                    raise web.HTTPTooManyRequests(text="登录请求过多，请一分钟后重试", headers={"Retry-After": "60"})
                self.login_rates[key] = (expires, used + 1)
            if request.method not in ("GET", "HEAD") or request.headers.get("Upgrade", "").lower() == "websocket":
                if request.headers.get("Origin") != self.origin:
                    raise web.HTTPForbidden(text="Origin is not allowed")
                if not request.path.startswith("/stream/"):
                    self.auth.check_browser(request.cookies.get(BROWSER_COOKIE), request.headers.get("X-CSRF-Token"))
            response = await handler(request)
            if not isinstance(response, web.WebSocketResponse):
                response.headers["Cache-Control"] = "no-store"
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
            return response
        except AuthError as exc:
            return web.json_response({"error": str(exc)}, status=401)
        except StreamPolicyError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "请求格式无效"}, status=400)

    def account(self, request) -> dict:
        return self.auth.authorize(request.cookies.get(BROWSER_COOKIE), request.cookies.get(SESSION_COOKIE))

    def require_lease(self, request) -> dict:
        account = self.account(request)
        if not self.lease or self.lease["session"] != request.cookies.get(SESSION_COOKIE):
            raise web.HTTPConflict(text="请先取得测试会话控制权")
        self.lease["expires"] = time.time() + 60
        return account

    async def startup(self, _app):
        self.client = ClientSession(timeout=ClientTimeout(total=10), auto_decompress=False)
        self.watchdog = asyncio.create_task(self.watch_leases())

    async def cleanup(self, _app):
        if self.watchdog:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
        await self.drop_lease()
        await self.client.close()

    async def watch_leases(self):
        while True:
            await asyncio.sleep(2)
            if self.lease:
                lease = self.lease
                try:
                    self.auth.authorize(lease["browser"], lease["session"])
                    if not lease.get("socket") and lease["expires"] <= time.time():
                        raise AuthError("控制租约过期")
                except AuthError:
                    async with self.lease_lock:
                        if self.lease is lease:
                            await self.drop_lease()

    async def provision(self, table: dict):
        async with self.client.post(self.backend + "/stream/api/tokens", json=table,
                                    headers={"Authorization": "Bearer " + self.master_token}) as response:
            if response.status != 200:
                raise web.HTTPBadGateway(text="媒体服务鉴权尚未就绪")

    async def drop_lease(self):
        if self.lease:
            old, self.lease = self.lease, None
            if old.get("socket"):
                await old["socket"].close(code=1008, message=b"lease ended")
            with contextlib.suppress(Exception):
                await self.provision({})

    async def index(self, request):
        return web.FileResponse(WEB_ROOT / "index.html")

    async def asset(self, request):
        return web.FileResponse(WEB_ROOT / request.path.lstrip("/"))

    async def health(self, request):
        return web.json_response({"service": "MultiCraft", "stage": "M0", "status": "ok"})

    async def bootstrap(self, request):
        browser, csrf = self.auth.browser(request.cookies.get(BROWSER_COOKIE))
        policy = load_policy(self.policy_file)
        response = web.json_response({"csrf": csrf, "service": self.origin, "limits": policy["limits"],
                                      "policyRevision": policy["revision"],
                                      "transport": "WebSocket / H.264 + Opus", "defaults": clamp_config(DEFAULT_CONFIG, policy["limits"])})
        response.set_cookie(BROWSER_COOKIE, browser, httponly=True, secure=self.secure, samesite="Strict", max_age=43200)
        return response

    async def challenge(self, request):
        data = await request.json()
        return web.json_response(self.auth.issue(request.cookies[BROWSER_COOKIE], data["publicKey"]))

    def login_response(self, result: dict):
        token = result.pop("sessionToken", None)
        response = web.json_response(result)
        if token:
            response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=self.secure, samesite="Strict", max_age=43200)
        return response

    async def verify(self, request):
        data = await request.json()
        browser = request.cookies[BROWSER_COOKIE]
        # Consume on the event loop before offloading OpenSSH. Parallel requests
        # cannot verify the same challenge, and a slow signer never stalls media.
        challenge = self.auth.consume_challenge(browser, data["attemptId"])
        async with self.verify_slots:
            await asyncio.to_thread(self.auth.check_signature, challenge, data["signature"])
        return self.login_response(self.auth.finish_verified(browser, challenge))

    async def select(self, request):
        data = await request.json()
        return self.login_response(self.auth.select(request.cookies[BROWSER_COOKIE], data["preAuthId"], data["accountId"]))

    async def me(self, request):
        return web.json_response({"account": self.account(request)})

    async def logout(self, request):
        self.account(request)
        async with self.lease_lock:
            if self.lease and self.lease["session"] == request.cookies.get(SESSION_COOKIE):
                await self.drop_lease()
        self.auth.logout(request.cookies.get(SESSION_COOKIE))
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE)
        return response

    async def state(self, request):
        account = self.account(request)
        state = read_json(self.state_file)
        policy = load_policy(self.policy_file)
        allowed = ("backendReady", "nativeReady", "minecraftVersion", "minecraftUsername", "error", "measured")
        return web.json_response({"account": account, "runtime": {k: state[k] for k in allowed if k in state},
                                  "stream": read_json(self.effective_file), "limits": policy["limits"], "policyRevision": policy["revision"],
                                  "transport": "WebSocket / H.264 + Opus",
                                  "hasLease": bool(self.lease and self.lease["session"] == request.cookies.get(SESSION_COOKIE))})

    async def acquire(self, request):
        account = self.account(request)
        if account.get("minecraftUsername") != self.minecraft_username:
            raise web.HTTPForbidden(text="该账号未分配到这个 M0 原版角色")
        async with self.lease_lock:
            token = request.cookies[SESSION_COOKIE]
            if self.lease and self.lease["session"] != token:
                raise web.HTTPConflict(text="M0 测试会话正由另一个浏览器控制")
            if not self.lease:
                media_token = secrets.token_urlsafe(32)
                await self.provision({media_token: {"role": "controller", "slot": 1, "mk_control": True}})
                self.lease = {"session": token, "browser": request.cookies[BROWSER_COOKIE],
                              "token": media_token, "socket": None, "expires": time.time() + 60}
            return web.json_response({"ok": True, "url": "/stream/"})

    async def release(self, request):
        async with self.lease_lock:
            self.require_lease(request)
            await self.drop_lease()
        return web.json_response({"ok": True})

    async def stream_config(self, request):
        self.require_lease(request)
        data = await request.json()
        async with self.apply_lock:
            self.require_lease(request)
            config = validate_config(data, load_limits(self.policy_file))
            return web.json_response(await self.apply_config(config, request))

    async def apply_config(self, config: dict, lease_request=None) -> dict:
        """Called under apply_lock. The OP transaction retains old policy until ACK."""
        if lease_request is not None:
            self.require_lease(lease_request)
        validate_config(config, load_limits(self.policy_file))
        self.generation += 1
        wanted = {"requestId": secrets.token_urlsafe(18), "generation": self.generation, "config": config}
        atomic_json(self.request_file, wanted)
        deadline = time.monotonic() + self.apply_timeout
        while time.monotonic() < deadline:
            if lease_request is not None:
                self.require_lease(lease_request)
            actual = read_json(self.effective_file)
            if actual.get("requestId") == wanted["requestId"] and actual.get("generation") == wanted["generation"]:
                if actual.get("error"):
                    raise web.HTTPBadGateway(text=json.dumps(actual), content_type="application/json")
                if actual.get("effective") != config:
                    raise web.HTTPBadGateway(text=json.dumps({"error": "媒体后端确认的参数不匹配", "actual": actual}), content_type="application/json")
                return actual
            await asyncio.sleep(0.1)
        raise web.HTTPGatewayTimeout(text=json.dumps({"error": "媒体后端未确认参数；请检查状态后再尝试", "requestId": wanted["requestId"]}), content_type="application/json")

    def require_op(self, request) -> dict:
        account = self.account(request)
        if not account.get("isOp"):
            raise web.HTTPForbidden(text="只有 OP asta 可以管理传流上限")
        return account

    async def get_limits(self, request):
        self.require_op(request)
        return web.json_response({**load_policy(self.policy_file), "absoluteMaximums": ABSOLUTE_MAXIMUMS})

    async def set_limits(self, request):
        self.require_op(request)
        data = await request.json()
        if not isinstance(data, dict) or "limits" not in data or set(data) - {"limits", "expectedRevision"}:
            raise StreamPolicyError("请求必须包含 limits，可附带 expectedRevision")
        proposed = validate_limits(data["limits"])
        if "expectedRevision" in data and (type(data["expectedRevision"]) is not int or data["expectedRevision"] < 0):
            raise StreamPolicyError("expectedRevision 必须为非负整数")
        async with self.apply_lock:
            self.require_op(request)
            previous = load_policy(self.policy_file)
            if data.get("expectedRevision", previous["revision"]) != previous["revision"]:
                raise web.HTTPConflict(text="传流上限已更新，请重新加载后再保存")
            if proposed == previous["limits"]:
                return web.json_response({**previous, "absoluteMaximums": ABSOLUTE_MAXIMUMS, "streamAdjusted": False})
            current = read_json(self.effective_file).get("effective") or clamp_config(DEFAULT_CONFIG, previous["limits"])
            adjusted = clamp_config(current, proposed)
            changed = current != adjusted
            if changed:
                # Crucially apply under the old policy, ACK first, then commit.
                # The media worker reloads that same file on every apply.
                await self.apply_config(adjusted)
            self.require_op(request)
            if load_policy(self.policy_file) != previous:
                raise web.HTTPConflict(text="限额文件在更新期间变化，请重新加载")
            policy = {"revision": previous["revision"] + 1, "limits": proposed}
            try:
                atomic_json(self.policy_file, policy)
            except OSError:
                raise web.HTTPInternalServerError(text="无法保存传流上限；旧限额保持有效") from None
            return web.json_response({**policy, "absoluteMaximums": ABSOLUTE_MAXIMUMS, "streamAdjusted": changed})

    async def proxy(self, request):
        self.require_lease(request)
        tail = request.match_info["tail"]
        decoded = unquote(unquote(tail))
        if ".." in decoded.split("/") or "\\" in decoded or decoded.startswith("/"):
            raise web.HTTPForbidden()
        is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
        if tail.startswith("api/") and tail not in ("api/websockets", "api/status", "api/health"):
            raise web.HTTPForbidden(text="该媒体接口未向 M0 开放")
        if request.method != "GET" or (is_ws and tail != "api/websockets"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        query = [(k, v) for k, v in request.query.items() if k not in ("token", "master_token")]
        headers = {"Authorization": "Bearer " + self.lease["token"], "Origin": self.backend_origin}
        url = self.backend + "/stream/" + tail
        if not is_ws:
            async with self.client.get(url, params=query, headers=headers, allow_redirects=False) as response:
                body = await response.read()
                forwarded = {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "content-encoding")}
                return web.Response(body=body, status=response.status, headers=forwarded)
        async with self.lease_lock:
            self.require_lease(request)
            lease = self.lease
            if lease.get("socket") or lease.get("connecting"):
                raise web.HTTPConflict(text="该测试会话已有一个媒体控制连接")
            connection_id = secrets.token_urlsafe(16)
            lease["connecting"] = connection_id
        upstream_protocol = base64.urlsafe_b64encode(lease["token"].encode()).decode().rstrip("=")
        try:
            upstream = await self.client.ws_connect(url, params=query, headers=headers,
                                                    protocols=["selkies", "selkies.token." + upstream_protocol],
                                                    max_msg_size=4 * 1024 * 1024, autoping=False)
        except Exception:
            async with self.lease_lock:
                if self.lease is lease and lease.get("connecting") == connection_id:
                    lease.pop("connecting", None)
            raise web.HTTPBadGateway(text="媒体连接失败") from None
        downstream = web.WebSocketResponse(max_msg_size=256 * 1024, autoping=False)
        async with self.lease_lock:
            if self.lease is not lease or lease.get("connecting") != connection_id:
                await upstream.close()
                raise web.HTTPConflict(text="控制租约已结束")
            try:
                await downstream.prepare(request)
            except Exception:
                lease.pop("connecting", None)
                await upstream.close()
                raise
            lease.pop("connecting", None)
            lease["socket"] = downstream

        async def relay(source, destination, from_browser=False):
            paced_at = time.monotonic()
            async for message in source:
                if from_browser:
                    self.require_lease(request)
                if message.type == WSMsgType.TEXT:
                    payload = self.filter_client_message(message.data) if from_browser else message.data
                    if payload is not None:
                        await destination.send_str(payload)
                elif message.type == WSMsgType.BINARY:
                    if from_browser:
                        # Selkies 0x05 is gzip control text. Apply exactly the same
                        # policy as TEXT; binary wrapping must not bypass controls.
                        text = self.decode_client_control(message.data)
                        payload = self.filter_client_message(text) if text is not None else None
                        if payload is not None:
                            await destination.send_str(payload)
                    else:
                        # Explicit operator-only test fixture. Disabled by
                        # default; no browser route can set it. Real encoded
                        # media is paced so ACKs exercise native congestion
                        # control over a genuinely constrained path.
                        rate = self.test_egress_rate()
                        if rate:
                            paced_at = max(time.monotonic(), paced_at) + len(message.data) * 8 / (rate * 1000)
                            await asyncio.sleep(max(0, paced_at - time.monotonic()))
                        else:
                            paced_at = time.monotonic()
                        await destination.send_bytes(message.data)
                elif message.type == WSMsgType.PING:
                    await destination.ping(message.data)
                elif message.type == WSMsgType.PONG:
                    await destination.pong(message.data)
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    break
        tasks = [asyncio.create_task(relay(downstream, upstream, True)), asyncio.create_task(relay(upstream, downstream))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await downstream.close()
            async with self.lease_lock:
                if self.lease is lease and lease.get("socket") is downstream:
                    lease["socket"] = None
                    lease["expires"] = time.time() + 30
        return downstream

    def test_egress_rate(self) -> int:
        if not self.test_egress_file:
            return 0
        try:
            rate = int(self.test_egress_file.read_text().strip())
            return rate if 64 <= rate <= 100000 else 0
        except (OSError, ValueError):
            return 0

    @staticmethod
    def decode_client_control(data: bytes) -> str | None:
        # Microphone/webcam uploads are outside this game-only M0 interface.
        if not data or data[0] != 0x05:
            return None
        try:
            decoder = zlib.decompressobj(31)
            raw = decoder.decompress(data[1:], 32769)
            if len(raw) > 32768 or decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
                return None
            return raw.decode("utf-8")
        except (zlib.error, UnicodeError):
            return None

    def filter_client_message(self, message: str) -> str | None:
        """Only the validated MultiCraft controls may alter M0 stream geometry/rates."""
        config = clamp_config(read_json(self.effective_file).get("effective") or DEFAULT_CONFIG, load_limits(self.policy_file))
        if message.startswith("SETTINGS,"):
            try:
                settings = json.loads(message.split(",", 1)[1])
                if not isinstance(settings, dict):
                    return None
            except ValueError:
                return None
            settings.update(framerate=config["fps"], video_bitrate=config["bitrateKbps"],
                            manual_resolution=True, manual_width=config["width"], manual_height=config["height"],
                            initialClientWidth=config["width"], initialClientHeight=config["height"],
                            displayId="primary", displayScale=1, scaling_dpi=96,
                            rate_control_mode="cbr", encoder="h264enc", video_fullcolor=False, video_10bit=False,
                            enable_binary_clipboard=False, use_cpu=False)
            return "SETTINGS," + json.dumps(settings, separators=(",", ":"))
        opcode = message.split(",", 1)[0]
        if opcode == "SET_NATIVE_CURSOR_RENDERING":
            return "SET_NATIVE_CURSOR_RENDERING,0"
        if opcode == "p":
            return "p,0"
        if opcode in ("vb", "_arg_fps", "r", "s", "cmd", "cb"):
            return None
        return message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://127.0.0.1:18081")
    parser.add_argument("--master-token-file", type=Path, required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--stream-request", type=Path, required=True)
    parser.add_argument("--stream-effective", type=Path, required=True)
    parser.add_argument("--runtime-state", type=Path)
    parser.add_argument("--minecraft-username", default="M0Player")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--insecure-loopback", action="store_true")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--test-egress-file", type=Path, help="operator-only bandwidth validation fixture; disabled by default")
    parser.add_argument("--policy-file", type=Path, help="shared OP limits; defaults beside stream-request as stream-policy.json")
    args = parser.parse_args()
    ssl_context = None
    if args.tls_cert:
        import ssl
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(args.tls_cert, args.tls_key)
    gateway = Gateway(backend=args.backend, master_token_file=args.master_token_file, accounts_file=args.accounts,
                      origin=args.origin, stream_request=args.stream_request, stream_effective=args.stream_effective,
                      runtime_state=args.runtime_state, minecraft_username=args.minecraft_username,
                      insecure_loopback=args.insecure_loopback, test_egress_file=args.test_egress_file, policy_file=args.policy_file)
    web.run_app(gateway.app, host=args.host, port=args.port, ssl_context=ssl_context,
                access_log=None, shutdown_timeout=5)


if __name__ == "__main__":
    main()
