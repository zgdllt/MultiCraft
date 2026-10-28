"""Public multi-instance gateway. Each browser tab has a scoped login namespace."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, UnixConnector, WSMsgType, web

from .auth import AuthError, AuthManager
from .pool_config import STREAM_BOUNDS
from .native_input import validate_input, validate_runtime_owner
from .stream_policy import StreamPolicyError, clamp_config, load_policy, validate_config, validate_limits

BROWSER_COOKIE = "multicraft_release_browser"
SESSION_COOKIE = "multicraft_release_session"
WEB = Path(__file__).resolve().parents[1] / "web"
BRAND = WEB.parent / "assets" / "brand"
TAB_PATTERN = r"[A-Za-z0-9_-]{24,64}"
ASSETS = {"app.js", "style.css", "viewer.html", "viewer.js", "viewer-input.js", "viewer-bridge.js", "m1-player.js"}


def release_limits(value: dict) -> dict:
    limits = validate_limits(value)
    for name, (_, maximum) in STREAM_BOUNDS.items():
        if limits[name][1] > maximum:
            raise StreamPolicyError(f"{name} 上限超过当前原生媒体后端支持的 {maximum}")
    return limits


def release_policy(path: Path) -> dict:
    policy = load_policy(path)
    release_limits(policy["limits"])
    return policy


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ReleaseGateway:
    def __init__(self, *, origin: str, accounts_file: Path, token_file: Path, media_socket: Path,
                 policy_file: Path, sessions: list[str], native_url="http://127.0.0.1:18091",
                 insecure_loopback=False):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or parsed.path not in {"", "/"} or parsed.query
                or parsed.fragment or parsed.username or not parsed.netloc):
            raise ValueError("an exact origin is required")
        if parsed.scheme != "https" and not (insecure_loopback and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("HTTPS is required except explicit loopback tests")
        endpoint = urlsplit(native_url)
        if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
                or endpoint.username or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
            raise ValueError("native upstream must be an HTTP loopback origin")
        if (not token_file.is_file() or token_file.is_symlink() or token_file.stat().st_mode & 0o077):
            raise ValueError("native token must be an owner-only regular file")
        self.token = token_file.read_text().strip()
        if len(self.token) < 32:
            raise ValueError("native token is too short")
        if (not 1 <= len(sessions) <= 16 or len(set(sessions)) != len(sessions)
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) for value in sessions)):
            raise ValueError("configure 1..16 unique native session IDs")
        self.origin, self.authority, self.secure = origin.rstrip("/"), parsed.netloc, parsed.scheme == "https"
        self.auth = AuthManager(accounts_file, self.origin)
        self.sessions = tuple(sessions)
        self._bindings()
        self.native_url, self.media_socket, self.policy_file = native_url.rstrip("/"), media_socket, policy_file
        release_policy(policy_file)
        self.tabs: dict[str, dict] = {}
        self.rates: dict[str, tuple[float, int]] = {}
        self.controllers: dict[str, dict] = {}
        self.input_locks = {slot: asyncio.Lock() for slot in sessions}
        self.apply_lock = asyncio.Lock()
        self.verify_slots = asyncio.Semaphore(4)
        self.client = self.media = None
        self.app = web.Application(client_max_size=32768, middlewares=[self.guard])
        prefix = "/t/{tab:" + TAB_PATTERN + "}"
        routes = [web.get("/", self.new_tab), web.get("/health", self.health), web.get(prefix + "/", self.index),
                  web.get(prefix + "/api/bootstrap", self.bootstrap),
                  web.post(prefix + "/api/auth/challenge", self.challenge), web.post(prefix + "/api/auth/verify", self.verify),
                  web.post(prefix + "/api/auth/select", self.select), web.get(prefix + "/api/me", self.me),
                  web.post(prefix + "/api/logout", self.logout), web.get(prefix + "/api/state", self.state),
                  web.post(prefix + "/api/lease", self.acquire), web.delete(prefix + "/api/lease", self.release),
                  web.post(prefix + "/api/session/{action:input|command}", self.control),
                  web.post(prefix + "/api/stream-config", self.configure),
                  web.get(prefix + "/api/op/stream-limits", self.get_limits),
                  web.put(prefix + "/api/op/stream-limits", self.set_limits),
                  web.get(prefix + "/media/{kind:video|audio}", self.proxy),
                  web.get(prefix + "/assets/brand/logo.png", self.logo), web.get(prefix + "/{asset}", self.asset)]
        self.app.add_routes(routes)
        self.app.on_startup.append(self.startup)
        self.app.on_shutdown.append(self.shutdown)
        self.app.on_cleanup.append(self.cleanup)

    def _bindings(self):
        accounts, _ = self.auth._registry()
        bindings = {account["id"]: account.get("nativeSession") for account in accounts}
        enabled = [account for account in accounts if account.get("enabled", True)]
        slots = [bindings[a["id"]] for a in enabled]
        if any(slot not in self.sessions for slot in slots) or len(slots) != len(set(slots)):
            raise ValueError("each enabled account requires its own configured nativeSession")
        return bindings

    @staticmethod
    def prefix(request):
        return "/t/" + request.match_info["tab"] + "/"

    def browser(self, request):
        tab = self.tabs.get(request.match_info.get("tab"))
        browser = request.cookies.get(BROWSER_COOKIE)
        if not tab or not browser or tab.get("browser") != browser:
            raise AuthError("此标签页的登录状态已失效，请打开新的身份标签页")
        return browser

    def account(self, request):
        return self.auth.authorize(self.browser(request), request.cookies.get(SESSION_COOKIE))

    def slot(self, request):
        account = self.account(request)
        slot = self._bindings().get(account["id"])
        if slot not in self.sessions:
            raise web.HTTPForbidden(text="当前账号没有已启用的客户端档案")
        return slot

    def require_op(self, request):
        if not self.account(request)["isOp"]:
            raise web.HTTPForbidden(text="仅配置的 OP 可管理传流上限")

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.host != self.authority:
                raise web.HTTPForbidden(text="Host is not allowed")
            websocket = request.headers.get("Upgrade", "").lower() == "websocket"
            if websocket or request.method not in {"GET", "HEAD"}:
                if request.headers.get("Origin") != self.origin:
                    raise web.HTTPForbidden(text="Origin is not allowed")
            if request.path == "/" or request.path.endswith("/api/bootstrap") or "/api/auth/" in request.path:
                now = time.monotonic()
                self.rates = {key: value for key, value in self.rates.items() if value[0] > now}
                peer = request.remote or "unknown"
                expiry, used = self.rates.get(peer, (now + 60, 0))
                if used >= 120 or (peer not in self.rates and len(self.rates) >= 2048):
                    raise web.HTTPTooManyRequests(text="登录请求过多，请稍后再试")
                self.rates[peer] = (expiry, used + 1)
            if request.method not in {"GET", "HEAD"}:
                self.auth.check_browser(self.browser(request), request.headers.get("X-CSRF-Token"))
            response = await handler(request)
        except AuthError as exc:
            response = web.json_response({"error": str(exc)}, status=401)
        except web.HTTPException as exc:
            if 300 <= exc.status < 400:
                response = web.Response(status=exc.status, headers=exc.headers)
            else:
                response = web.json_response({"error": exc.text}, status=exc.status)
        except (ValueError, TypeError, KeyError, StreamPolicyError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except (ClientError, OSError, asyncio.TimeoutError):
            response = web.json_response({"error": "客户端运行时或媒体后端暂不可用"}, status=503)
        if not response.prepared:
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'self'"})
        return response

    async def startup(self, _):
        self.client = ClientSession(timeout=ClientTimeout(total=20), cookie_jar=DummyCookieJar())
        self.media = ClientSession(connector=UnixConnector(path=str(self.media_socket)), timeout=ClientTimeout(total=None))

    async def shutdown(self, _):
        for slot, lease in list(self.controllers.items()):
            with contextlib.suppress(ClientError, OSError, asyncio.TimeoutError, web.HTTPException):
                await self.drop_controller(slot, lease)

    async def cleanup(self, _):
        for client in (self.media, self.client):
            if client:
                await client.close()

    async def new_tab(self, _):
        # The opaque route is a namespace, not an authentication credential.
        self.auth._prune()
        self.tabs = {key: value for key, value in self.tabs.items() if value.get("browser") in self.auth.browsers or value["expires"] > time.monotonic()}
        if len(self.tabs) >= 2048:
            raise web.HTTPTooManyRequests(text="标签页过多，请稍后重试")
        tab = secrets.token_urlsafe(24)
        self.tabs[tab] = {"expires": time.monotonic() + 600}
        raise web.HTTPFound("/t/" + tab + "/")

    async def index(self, request):
        if request.match_info["tab"] not in self.tabs:
            raise web.HTTPFound("/")
        return web.FileResponse(WEB / "index.html")

    async def asset(self, request):
        asset = request.match_info["asset"]
        if asset not in ASSETS:
            raise web.HTTPNotFound()
        if asset == "viewer.html":
            self.slot(request)
        return web.FileResponse(WEB / asset)

    async def logo(self, _):
        return web.FileResponse(BRAND / "logo.png")

    async def health(self, _):
        return web.json_response({"service": "MultiCraft", "stage": "alpha", "status": "ok"})

    async def bootstrap(self, request):
        tab = self.tabs.get(request.match_info["tab"])
        if not tab:
            raise AuthError("请从入口打开新的身份标签页")
        old = request.cookies.get(BROWSER_COOKIE)
        browser, csrf = self.auth.browser(old if old == tab.get("browser") else None)
        tab["browser"] = browser
        policy = release_policy(self.policy_file)
        response = web.json_response({"csrf": csrf, "service": self.origin, "limits": policy["limits"], "policyRevision": policy["revision"], "transport": "WebSocket / H.264 + PCM"})
        response.set_cookie(BROWSER_COOKIE, browser, path=self.prefix(request), httponly=True, secure=self.secure, samesite="Strict", max_age=43200)
        return response

    async def challenge(self, request):
        return web.json_response(self.auth.issue(self.browser(request), (await request.json())["publicKey"]))

    async def login_response(self, request, result):
        token = result.pop("sessionToken", None)
        if token:
            await self.drop_owned(request.cookies.get(SESSION_COOKIE))
        response = web.json_response(result)
        if token:
            response.set_cookie(SESSION_COOKIE, token, path=self.prefix(request), httponly=True, secure=self.secure, samesite="Strict", max_age=43200)
        return response

    async def verify(self, request):
        data = await request.json()
        browser = self.browser(request)
        challenge = self.auth.consume_challenge(browser, data["attemptId"])
        async with self.verify_slots:
            await asyncio.to_thread(self.auth.check_signature, challenge, data["signature"])
        return await self.login_response(request, self.auth.finish_verified(browser, challenge))

    async def select(self, request):
        data = await request.json()
        return await self.login_response(request, self.auth.select(self.browser(request), data["preAuthId"], data["accountId"]))

    async def me(self, request):
        return web.json_response({"account": self.account(request)})

    async def logout(self, request):
        self.browser(request)
        await self.drop_owned(request.cookies.get(SESSION_COOKIE))
        self.auth.logout(request.cookies.get(SESSION_COOKIE))
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path=self.prefix(request))
        return response

    async def native(self, method, path, value=None):
        async with self.client.request(method, self.native_url + path, json=value, allow_redirects=False,
                headers={"Authorization": "Bearer " + self.token}) as response:
            data = await response.content.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                raise web.HTTPBadGateway(text="native response too large")
            result = json.loads(data)
            if response.status >= 400:
                raise web.HTTPConflict(text=result.get("error", "原生客户端拒绝了过时代际的请求"))
            return result

    async def media_request(self, method, path, value=None):
        async with self.media.request(method, "http://localhost" + path, json=value,
                                      timeout=ClientTimeout(total=25)) as response:
            data = await response.json()
            if response.status >= 400 or data.get("applied") is False:
                raise web.HTTPConflict(text=data.get("error", "媒体参数未成功生效"))
            return data

    @staticmethod
    def media_slot(status, slot):
        table = status.get("sessions", status.get("video", {}))
        if isinstance(table, list):
            return next((value for value in table if value.get("id", value.get("session")) == slot), {})
        return table.get(slot, {})

    async def state(self, request):
        slot = self.slot(request)
        native, media = await asyncio.gather(self.native("GET", "/state"), self.media_request("GET", "/status"))
        value = next((item for item in native.get("sessions", []) if item["id"] == slot), None)
        if value is None:
            raise web.HTTPServiceUnavailable(text="当前客户端档案尚未启动")
        policy = release_policy(self.policy_file)
        lease = self.controllers.get(slot)
        own = bool(lease and lease["owner"] == request.cookies.get(SESSION_COOKIE) and not lease["closing"])
        return web.json_response({"account": self.account(request), "native": value, "runtimeOwner": native.get("runtimeOwner"),
            "runtime": {"nativeReady": not value.get("closed") and not value.get("failure"), "error": value.get("failure")},
            "stream": self.media_slot(media, slot), "limits": policy["limits"], "policyRevision": policy["revision"],
            "controller": lease["generation"] if own else None, "hasLease": own, "transport": "WebSocket / H.264 + PCM"})

    async def acquire(self, request):
        slot = self.slot(request)
        lease = self.controllers.get(slot)
        if lease:
            raise web.HTTPConflict(text="此身份已有画面连接，请先在原标签页释放，或打开新身份标签页")
        return web.json_response({"ok": True, "url": self.prefix(request) + "viewer.html"})

    async def release(self, request):
        self.slot(request)
        await self.drop_owned(request.cookies.get(SESSION_COOKIE))
        return web.json_response({"ok": True})

    async def drop_owned(self, owner):
        if not owner:
            return
        for slot, lease in list(self.controllers.items()):
            if lease["owner"] == owner:
                await self.drop_controller(slot, lease)

    async def drop_controller(self, slot, lease):
        if self.controllers.get(slot) is not lease:
            return
        lease["closing"] = True
        for socket in list(lease["sockets"]):
            await socket.close(code=1000, message=b"control released")
        async with self.input_locks[slot]:
            if self.controllers.get(slot) is lease:
                try:
                    state = await self.native("GET", "/state")
                    current = next((item for item in state.get("sessions", []) if item.get("id") == slot), None)
                    # An old socket may finish after an entirely new native host
                    # or network connection starts. It can release only its own
                    # saved owner/epoch, never infer ownership from fresh state.
                    if (current and state.get("runtimeOwner") == lease["runtimeOwner"]
                            and current.get("connectionEpoch") == lease["nativeEpoch"]
                            and not current.get("closed") and not current.get("failure")):
                        last_seq = current.get("lastInputSeq")
                        if type(last_seq) is not int or not 0 <= last_seq < 2**53 - 1:
                            raise web.HTTPBadGateway(text="native input sequence is invalid")
                        await self.native("POST", f"/session/{slot}/input", {
                            "expectedRuntimeOwner": lease["runtimeOwner"], "expectedEpoch": lease["nativeEpoch"],
                            "inputSeq": last_seq + 1, "events": [{"type": "release"}]})
                finally:
                    self.controllers.pop(slot, None)

    def require_controller(self, request, slot):
        lease = self.controllers.get(slot)
        generation = request.headers.get("X-MultiCraft-Control", "")
        if (not lease or lease["closing"] or lease["owner"] != request.cookies.get(SESSION_COOKIE)
                or not secrets.compare_digest(lease["generation"], generation)):
            raise web.HTTPConflict(text="请先连接画面并等待控制权确认")
        return lease

    async def control(self, request):
        slot, action = self.slot(request), request.match_info["action"]
        value = await request.json()
        if action == "input":
            value = validate_input(value)
        else:
            if not isinstance(value, dict) or set(value) != {"type", "expectedRuntimeOwner", "expectedEpoch"} or value["type"] not in {"connect", "disconnect", "respawn"}:
                raise ValueError("unsupported command")
            validate_runtime_owner(value["expectedRuntimeOwner"])
            if type(value["expectedEpoch"]) is not int or not 0 <= value["expectedEpoch"] <= 2**53 - 1:
                raise ValueError("invalid expectedEpoch")
        async with self.input_locks[slot]:
            if self.slot(request) != slot:
                raise web.HTTPConflict(text="账号客户端绑定已改变，请重新连接")
            lease = self.require_controller(request, slot)
            if (value["expectedRuntimeOwner"] != lease["runtimeOwner"]
                    or value["expectedEpoch"] != lease["nativeEpoch"]):
                raise web.HTTPConflict(text="原生连接代际已改变，请释放画面后重新连接")
            result = await self.native("POST", f"/session/{slot}/{action}", value)
            if action == "command":
                # Only this lease's successfully fenced command may advance its
                # epoch. A state poll must never adopt an external generation.
                acknowledged_epoch = result.get("connectionEpoch")
                if type(acknowledged_epoch) is not int or not lease["nativeEpoch"] <= acknowledged_epoch <= 2**53 - 1:
                    raise web.HTTPBadGateway(text="native command omitted its connection epoch ACK")
                lease["nativeEpoch"] = acknowledged_epoch
            return web.json_response(result)

    async def configure(self, request):
        slot = self.slot(request)
        value = await request.json()
        async with self.apply_lock:
            # Recheck key revocation and current registry after waiting for another transaction.
            if self.slot(request) != slot:
                raise web.HTTPConflict(text="档案已改变，请刷新")
            config = validate_config(value, release_policy(self.policy_file)["limits"])
            state = self.media_slot(await self.media_request("GET", "/status"), slot)
            result = await self.apply_config(slot, config, state)
            return web.json_response(result)

    async def apply_config(self, slot, config, state):
        request_id, expected_generation = secrets.token_urlsafe(18), state["generation"]
        result = await self.media_request("POST", f"/session/{slot}/config", {"requestId": request_id,
                    "expectedGeneration": expected_generation, "config": config})
        if (result.get("applied") is not True or result.get("effective") != config
                or result.get("requestId") != request_id
                or type(result.get("generation")) is not int or result["generation"] != expected_generation + 1):
            raise web.HTTPBadGateway(text="媒体未确认本次请求及精确参数代际")
        return result

    async def get_limits(self, request):
        self.require_op(request)
        return web.json_response(release_policy(self.policy_file))

    async def set_limits(self, request):
        self.require_op(request)
        value = await request.json()
        if not isinstance(value, dict) or set(value) != {"limits", "expectedRevision"} or type(value["expectedRevision"]) is not int:
            raise ValueError("requires limits and expectedRevision")
        limits = release_limits(value["limits"])
        async with self.apply_lock:
            self.require_op(request)
            policy = release_policy(self.policy_file)
            if value["expectedRevision"] != policy["revision"]:
                raise web.HTTPConflict(text="上限版本已改变，请重新加载")
            status = await self.media_request("GET", "/status")
            updates = []
            try:
                for slot in self.sessions:
                    previous = self.media_slot(status, slot)
                    config = clamp_config(previous["effective"], limits)
                    if config != previous["effective"]:
                        updates.append((slot, previous["effective"]))
                        await self.apply_config(slot, config, previous)
                self.require_op(request)
                # The old policy remains authoritative until every ACK has arrived.
                if release_policy(self.policy_file) != policy:
                    raise web.HTTPConflict(text="上限文件已被外部修改，请重新加载")
                updated = {"revision": policy["revision"] + 1, "limits": limits}
                atomic_json(self.policy_file, updated)
            except Exception as original:
                failures = []
                for slot, old_config in reversed(updates):
                    try:
                        current = self.media_slot(await self.media_request("GET", "/status"), slot)
                        await self.apply_config(slot, old_config, current)
                    except Exception:
                        failures.append(slot)
                if failures:
                    # Old caps still hold; current values are all at or below them.
                    raise web.HTTPServiceUnavailable(text="上限未提交；部分传流回滚失败，请 OP 检查媒体状态") from original
                raise
        return web.json_response({**updated, "streamAdjusted": bool(updates)})

    async def proxy(self, request):
        slot, kind = self.slot(request), request.match_info["kind"]
        owner = request.cookies.get(SESSION_COOKIE)
        downstream = web.WebSocketResponse(max_msg_size=4096, heartbeat=5)
        lease, upstream = None, None
        try:
            async with self.input_locks[slot]:
                current = self.controllers.get(slot)
                if kind == "video":
                    if current:
                        raise web.HTTPConflict(text="该身份已有一个画面控制连接，请先释放")
                    state = await self.native("GET", "/state")
                    native_slot = next((item for item in state.get("sessions", []) if item.get("id") == slot), None)
                    runtime_owner = state.get("runtimeOwner")
                    validate_runtime_owner(runtime_owner)
                    if (not native_slot or native_slot.get("closed") or native_slot.get("failure")
                            or type(native_slot.get("connectionEpoch")) is not int
                            or not 0 <= native_slot["connectionEpoch"] <= 2**53 - 1):
                        raise web.HTTPServiceUnavailable(text="原生客户端尚未准备就绪")
                    if self.slot(request) != slot:
                        raise web.HTTPConflict(text="账号客户端绑定已改变，请重新连接")
                    lease = {"owner": owner, "generation": secrets.token_urlsafe(24), "closing": False,
                             "sockets": {downstream}, "runtimeOwner": runtime_owner,
                             "nativeEpoch": native_slot["connectionEpoch"]}
                    self.controllers[slot] = lease
                else:
                    if not current or current["closing"] or current["owner"] != owner:
                        raise web.HTTPConflict(text="请先连接当前身份的画面")
                    lease = current
                    lease["sockets"].add(downstream)
            upstream = await asyncio.wait_for(self.media.ws_connect(f"http://localhost/{kind}/{slot}", max_msg_size=4 * 1024 * 1024), 5)
            await downstream.prepare(request)
            if kind == "video":
                await downstream.send_json({"type": "control", "generation": lease["generation"]})

            async def receive():
                async for message in upstream:
                    if message.type == WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)
                    elif message.type == WSMsgType.TEXT:
                        await downstream.send_str(message.data)
                    else:
                        break

            async def send():
                async for message in downstream:
                    if kind != "video" or message.type != WSMsgType.TEXT:
                        break
                    value = json.loads(message.data)
                    if value == {"type": "keyframe"}:
                        await upstream.send_json(value)
                    elif (isinstance(value, dict) and set(value) == {"type", "frames", "timestampUs", "generation"}
                          and value["type"] == "decoded" and all(type(value[name]) is int and 0 <= value[name] <= 2**53 - 1 for name in ("frames", "timestampUs", "generation"))):
                        await upstream.send_json(value)
                    else:
                        break

            async def authorize():
                while True:
                    await asyncio.sleep(2)
                    if self.slot(request) != slot or self.controllers.get(slot) is not lease or lease["closing"]:
                        return

            tasks = [asyncio.create_task(receive()), asyncio.create_task(send()), asyncio.create_task(authorize())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if upstream:
                await upstream.close()
            if lease:
                lease["sockets"].discard(downstream)
                if kind == "video":
                    await self.drop_controller(slot, lease)
            if downstream.prepared:
                await downstream.close()
        return downstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("multicraft.json"))
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--insecure-loopback", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    config = json.loads((root / args.config).read_text())
    runtime = root / config.get("runtimeDirectory", ".runtime/pool")
    gateway = ReleaseGateway(origin=args.origin, accounts_file=root / config.get("accountsFile", ".runtime/accounts.json"),
        token_file=runtime / "control.token", media_socket=runtime / "media/control.sock",
        policy_file=root / config.get("policyFile", ".runtime/stream-policy.json"),
        sessions=[value["id"] for value in config["sessions"]], native_url=f"http://127.0.0.1:{config.get('controlPort', 18091)}",
        insecure_loopback=args.insecure_loopback)
    context = None
    if not args.insecure_loopback:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(root / config.get("tlsCertificate", ".runtime/tls/server.crt"), root / config.get("tlsKey", ".runtime/tls/server.key"))
    web.run_app(gateway.app, host="127.0.0.1" if args.insecure_loopback else "0.0.0.0", port=args.port, ssl_context=context, access_log=None)


if __name__ == "__main__":
    main()
