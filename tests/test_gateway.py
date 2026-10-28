import asyncio
import base64
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from multicraft.gateway import Gateway, atomic_json
from multicraft.stream_policy import DEFAULT_CONFIG, LIMITS, load_policy


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.public_key = {"format": "spki", "key": base64.b64encode(public).decode()}
        (self.root / "accounts.json").write_text(json.dumps({"operatorAccountId": "operator", "accounts": [
            {"id": "operator", "linuxUser": "asta", "minecraftUsername": "M0Player", "publicKeys": [self.public_key]}]}))
        token_file = self.root / "master-token"
        token_file.write_text("a" * 40)
        token_file.chmod(0o600)
        self.tables = []
        self.ws_gate = None
        async def provision(request):
            self.tables.append(await request.json())
            return web.Response(text="OK")
        backend = web.Application()
        backend.router.add_post("/stream/api/tokens", provision)
        async def compressed(_request):
            return web.Response(body=gzip.compress(b"console.log('real compressed JavaScript');"),
                                headers={"Content-Encoding": "gzip", "Content-Type": "application/javascript"})
        async def socket(request):
            if self.ws_gate:
                await self.ws_gate.wait()
            ws = web.WebSocketResponse(protocols=["selkies"])
            await ws.prepare(request)
            async for message in ws:
                if message.type == web.WSMsgType.TEXT:
                    await ws.send_str(message.data)
            return ws
        backend.router.add_get("/stream/assets/compressed.js", compressed)
        backend.router.add_get("/stream/api/websockets", socket)
        self.backend = TestServer(backend)
        await self.backend.start_server()
        self.gateway = Gateway(backend=str(self.backend.make_url("")).rstrip("/"), master_token_file=token_file,
                               accounts_file=self.root / "accounts.json", origin="http://localhost:18080",
                               stream_request=self.root / "request.json", stream_effective=self.root / "effective.json",
                               insecure_loopback=True, apply_timeout=0.3)
        self.server = TestServer(self.gateway.app)
        self.client = TestClient(self.server, cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.host = {"Host": "localhost:18080"}
        async with self.client.get("/api/bootstrap", headers=self.host) as response:
            self.csrf = (await response.json())["csrf"]
        self.headers = dict(self.host, Origin="http://localhost:18080", **{"X-CSRF-Token": self.csrf})

    async def asyncTearDown(self):
        await self.client.close()
        await self.backend.close()
        self.temp.cleanup()

    async def login(self):
        response = await self.client.post("/api/auth/challenge", headers=self.headers, json={"publicKey": self.public_key})
        challenge = await response.json()
        signature = base64.b64encode(self.key.sign(base64.b64decode(challenge["challengeBase64"]))).decode()
        response = await self.client.post("/api/auth/verify", headers=self.headers,
                                          json={"attemptId": challenge["attemptId"], "signature": signature})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["account"]["isOp"])

    async def test_anonymous_media_csrf_and_origin_are_denied(self):
        response = await self.client.get("/stream/", headers=self.host)
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/auth/challenge", headers=self.host, json={"publicKey": self.public_key})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/auth/challenge", headers=dict(self.headers, **{"X-CSRF-Token": "wrong"}), json={"publicKey": self.public_key})
        self.assertEqual(response.status, 401)

    async def test_login_request_rate_is_bounded(self):
        statuses = []
        for _ in range(32):
            response = await self.client.get("/api/bootstrap", headers=self.host)
            statuses.append(response.status)
            await response.read()
        self.assertIn(429, statuses)

    async def test_lease_does_not_expose_master_and_protects_backend_control_paths(self):
        await self.login()
        response = await self.client.post("/api/lease", headers=self.headers, json={})
        data = await response.json()
        self.assertEqual(data, {"ok": True, "url": "/stream/"})
        self.assertEqual(len(self.tables[-1]), 1)
        for path in ["/stream/api/tokens", "/stream/api/files/foo", "/stream/api/upload"]:
            response = await self.client.get(path, headers=self.host)
            self.assertEqual(response.status, 403)
        response = await self.client.post("/api/logout", headers=self.headers, json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.tables[-1], {})

    async def test_parameter_ack_and_timeout_and_limits(self):
        await self.login()
        await self.client.post("/api/lease", headers=self.headers, json={})
        response = await self.client.post("/api/stream-config", headers=self.headers, json=dict(DEFAULT_CONFIG, fps=1001))
        self.assertEqual(response.status, 400)
        async def acknowledge():
            while not (self.root / "request.json").exists():
                await asyncio.sleep(0.01)
            wanted = json.loads((self.root / "request.json").read_text())
            atomic_json(self.root / "effective.json", dict(requestId=wanted["requestId"], generation=wanted["generation"],
                                                          requested=wanted["config"], effective=wanted["config"], measured={"ready": False}, error=None))
        task = asyncio.create_task(acknowledge())
        response = await self.client.post("/api/stream-config", headers=self.headers, json=DEFAULT_CONFIG)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["measured"]["ready"])
        await task
        response = await self.client.post("/api/stream-config", headers=self.headers, json=dict(DEFAULT_CONFIG, fps=20))
        self.assertEqual(response.status, 504)

    async def test_settings_side_channel_cannot_bypass_policy(self):
        result = self.gateway.filter_client_message('SETTINGS,{"framerate":1000,"manual_width":99999,"video_bitrate":1000000,"displayId":"secondary"}')
        settings = json.loads(result.split(",", 1)[1])
        self.assertEqual(settings["framerate"], 30)
        self.assertEqual(settings["manual_width"], 1280)
        self.assertEqual(settings["video_bitrate"], 8000)
        self.assertEqual(settings["displayId"], "primary")
        self.assertIsNone(self.gateway.filter_client_message("vb,1000000"))
        self.assertEqual(self.gateway.filter_client_message("kd,119"), "kd,119")
        self.assertEqual(self.gateway.filter_client_message("SET_NATIVE_CURSOR_RENDERING,1"), "SET_NATIVE_CURSOR_RENDERING,0")
        self.assertEqual(self.gateway.filter_client_message("p,1"), "p,0")
        self.assertEqual(self.gateway.decode_client_control(b"\x05" + gzip.compress(b"kd,119")), "kd,119")
        self.assertIsNone(self.gateway.decode_client_control(b"\x05" + gzip.compress(b"x" * 32769)))
        self.assertIsNone(self.gateway.decode_client_control(b"\x02microphone"))

    async def test_compressed_static_assets_preserve_wire_encoding(self):
        await self.login()
        await self.client.post("/api/lease", headers=self.headers, json={})
        response = await self.client.get("/stream/assets/compressed.js", headers=self.host)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "console.log('real compressed JavaScript');")

    async def test_two_concurrent_socket_handshakes_cannot_share_control(self):
        await self.login()
        await self.client.post("/api/lease", headers=self.headers, json={})
        self.ws_gate = asyncio.Event()
        first = asyncio.create_task(self.client.ws_connect("/stream/api/websockets", headers=self.headers))
        for _ in range(100):
            if self.gateway.lease.get("connecting"):
                break
            await asyncio.sleep(0.01)
        response = await self.client.get("/stream/api/websockets", headers=dict(self.headers, Upgrade="websocket"))
        self.assertEqual(response.status, 409)
        self.ws_gate.set()
        ws = await first
        await ws.send_str("kd,119")
        self.assertEqual((await ws.receive()).data, "kd,119")
        await ws.send_bytes(b"\x05" + gzip.compress(b'SETTINGS,{"framerate":1000}'))
        normalized = (await ws.receive()).data
        self.assertEqual(json.loads(normalized.split(",", 1)[1])["framerate"], 30)
        await ws.close()

    async def acknowledge_generation(self, generation, *, error=None, observed_policies=None):
        for _ in range(100):
            path = self.root / "request.json"
            if path.exists():
                wanted = json.loads(path.read_text())
                if wanted["generation"] == generation:
                    if observed_policies is not None:
                        observed_policies.append(load_policy(self.gateway.policy_file))
                    atomic_json(self.root / "effective.json", dict(requestId=wanted["requestId"], generation=generation,
                                                                  requested=wanted["config"], effective=wanted["config"] if not error else DEFAULT_CONFIG,
                                                                  measured={"ready": False}, error=error))
                    return wanted
            await asyncio.sleep(0.01)
        raise AssertionError("no matching media request")

    async def test_only_op_can_manage_limits_even_without_a_control_lease(self):
        await self.login()
        response = await self.client.get("/api/op/stream-limits", headers=self.host)
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.gateway.lease)
        registry = json.loads((self.root / "accounts.json").read_text())
        registry["operatorAccountId"] = None
        (self.root / "accounts.json").write_text(json.dumps(registry))
        for method in ("get", "put"):
            response = await getattr(self.client, method)("/api/op/stream-limits", headers=self.headers,
                                                          **({"json": {"limits": LIMITS}} if method == "put" else {}))
            self.assertEqual(response.status, 403)

    async def test_op_increase_is_persisted_without_restarting_or_claiming_control(self):
        await self.login()
        limits = dict(LIMITS, width=[320, 2560], height=[180, 1440], bitrateKbps=[500, 30000])
        response = await self.client.put("/api/op/stream-limits", headers=self.headers,
                                          json={"limits": limits, "expectedRevision": 0})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result["streamAdjusted"])
        self.assertEqual(load_policy(self.gateway.policy_file), {"revision": 1, "limits": limits})
        self.assertFalse((self.root / "request.json").exists())
        self.assertIsNone(self.gateway.lease)
        state = await (await self.client.get("/api/state", headers=self.host)).json()
        self.assertEqual(state["limits"], limits)
        bootstrap = await (await self.client.get("/api/bootstrap", headers=self.host)).json()
        self.assertEqual(bootstrap["limits"], limits)
        response = await self.client.put("/api/op/stream-limits", headers=self.headers,
                                          json={"limits": LIMITS, "expectedRevision": 0})
        self.assertEqual(response.status, 409)

    async def test_op_lowering_applies_under_old_policy_then_commits(self):
        await self.login()
        atomic_json(self.root / "effective.json", {"generation": 0, "effective": DEFAULT_CONFIG})
        limits = dict(LIMITS, width=[320, 800], height=[180, 600], fps=[10, 20], bitrateKbps=[500, 1500])
        observed = []
        ack = asyncio.create_task(self.acknowledge_generation(1, observed_policies=observed))
        response = await self.client.put("/api/op/stream-limits", headers=self.headers,
                                          json={"limits": limits, "expectedRevision": 0})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["streamAdjusted"])
        wanted = await ack
        self.assertEqual(observed, [{"revision": 0, "limits": LIMITS}])
        self.assertEqual(wanted["config"], dict(DEFAULT_CONFIG, width=800, height=600, fps=20, bitrateKbps=1500))
        self.assertEqual(load_policy(self.gateway.policy_file), {"revision": 1, "limits": limits})
        self.assertIsNone(self.gateway.lease)

    async def test_op_lowering_failure_keeps_old_persistent_policy(self):
        await self.login()
        limits = dict(LIMITS, width=[320, 640])
        ack = asyncio.create_task(self.acknowledge_generation(1, error="capture unavailable"))
        response = await self.client.put("/api/op/stream-limits", headers=self.headers, json={"limits": limits})
        self.assertEqual(response.status, 502)
        await ack
        self.assertEqual(load_policy(self.gateway.policy_file), {"revision": 0, "limits": LIMITS})
        self.assertFalse(self.gateway.policy_file.exists())

    async def test_waiting_player_configuration_rechecks_the_committed_limits(self):
        await self.login()
        await self.client.post("/api/lease", headers=self.headers, json={})
        limits = dict(LIMITS, width=[320, 640], height=[180, 360], bitrateKbps=[500, 2000])
        lower = asyncio.create_task(self.client.put("/api/op/stream-limits", headers=self.headers, json={"limits": limits}))
        while self.gateway.generation < 1:
            await asyncio.sleep(0.01)
        player = asyncio.create_task(self.client.post("/api/stream-config", headers=self.headers, json=DEFAULT_CONFIG))
        await self.acknowledge_generation(1)
        self.assertEqual((await lower).status, 200)
        self.assertEqual((await player).status, 400)

    async def test_waiting_configuration_cannot_mutate_media_after_lease_release(self):
        await self.login()
        await self.client.post("/api/lease", headers=self.headers, json={})
        await self.gateway.apply_lock.acquire()
        try:
            pending = asyncio.create_task(self.client.post("/api/stream-config", headers=self.headers, json=DEFAULT_CONFIG))
            await asyncio.sleep(0.03)
            response = await self.client.delete("/api/lease", headers=self.headers)
            self.assertEqual(response.status, 200)
        finally:
            self.gateway.apply_lock.release()
        self.assertEqual((await pending).status, 409)
        self.assertEqual(self.gateway.generation, 0)
        self.assertFalse((self.root / "request.json").exists())


if __name__ == "__main__":
    unittest.main()
