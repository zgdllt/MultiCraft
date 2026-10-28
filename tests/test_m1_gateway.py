import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from multicraft.m1_gateway import M1Gateway, validate_input
from multicraft.stream_policy import LIMITS


def envelope(events, seq=3):
    return {"expectedRuntimeOwner": "a" * 32, "expectedEpoch": 2, "inputSeq": seq, "events": events}


class M1InputTests(unittest.TestCase):
    def test_flat_bounded_input_is_validated_before_submission(self):
        valid = envelope([{"type": "key", "scancode": 26, "keycode": 119, "action": 1}, {"type": "release"}])
        self.assertEqual(validate_input(valid), valid)
        invalid_events = ([{"events": [{"type": "release"}]}], [{"type": "release"}] * 129,
                          [{"type": "key", "scancode": True, "keycode": 1, "action": 1}],
                          [{"type": "move", "x": float("nan"), "y": 0}],
                          [{"type": "grab", "command": "shutdown"}],
                          [{"type": "text", "text": "x" * 16385}])
        invalids = [envelope(events) for events in invalid_events]
        invalids += [{"events": [{"type": "release"}]},
                     {"expectedEpoch": True, "inputSeq": 1, "events": [{"type": "release"}]}]
        for invalid in invalids:
            with self.subTest(invalid=str(invalid)[:100]), self.assertRaises(ValueError):
                validate_input(invalid)


class M1GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = "private-host-token-" + "x" * 40
        token_file = self.root / "token"
        token_file.write_text(self.token)
        token_file.chmod(0o600)
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"revision": 0, "limits": LIMITS}))
        self.account = {"id": "asta-m0", "linuxUser": "asta", "isOp": True}
        self.auth_requests = []
        self.controls = []
        self.feedback = []
        self.sockets = []
        self.held = {"A": set(), "B": set()}

        async def identity(request):
            self.auth_requests.append({"cookies": dict(request.cookies), "host": request.host})
            if request.cookies.get("multicraft_session") != "valid-session":
                raise web.HTTPUnauthorized()
            return web.json_response({"account": self.account})

        auth = web.Application()
        auth.router.add_get("/api/me", identity)
        self.auth = TestServer(auth)
        await self.auth.start_server()

        async def native(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + self.token)
            if request.path == "/state":
                return web.json_response({"pid": 123, "runtimeOwner": "a" * 32, "sessions": [{"id": "A"}, {"id": "B"}], "audioMode": "disabled-probe"})
            value = await request.json()
            if request.path.endswith("/command") and value.get("expectedEpoch") != 2:
                return web.json_response({"error": "stale connection epoch"}, status=400)
            self.controls.append((request.path, value))
            if request.path.endswith("/input"):
                held = self.held[request.match_info["session"]]
                for event in value.get("events", [value]):
                    if event["type"] == "release":
                        held.clear()
                    elif event["type"] == "key":
                        (held.add if event["action"] else held.discard)(event["scancode"])
            return web.json_response({"ok": True})

        backend = web.Application()
        backend.router.add_get("/state", native)
        backend.router.add_post("/session/{session}/{action}", native)
        self.backend = TestServer(backend)
        await self.backend.start_server()

        async def media(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            session = request.match_info["session"]
            await ws.send_json({"session": session, "type": request.match_info["kind"]})
            await ws.send_bytes(("frame-" + session).encode())
            async for value in ws:
                self.feedback.append((session, value.data))
            return ws

        app = web.Application()
        app.router.add_get("/{kind}/{session}", media)
        self.media_runner = web.AppRunner(app)
        await self.media_runner.setup()
        media_socket = self.root / "media.sock"
        await web.UnixSite(self.media_runner, str(media_socket)).start()
        self.gateway = M1Gateway(origin="http://localhost:18092", auth_url=str(self.auth.make_url("")),
                                 auth_authority="expected-m0:18080", certificate=None,
                                 token_file=token_file, media_socket=media_socket, policy_file=self.policy,
                                 native_url=str(self.backend.make_url("")), insecure_loopback=True)
        self.client = TestClient(TestServer(self.gateway.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Host": "localhost:18092", "Origin": "http://localhost:18092"}

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        await self.client.close()
        await self.media_runner.cleanup()
        await self.backend.close()
        await self.auth.close()
        self.temp.cleanup()

    async def login(self):
        self.client.session.cookie_jar.update_cookies({"multicraft_browser": "valid-browser",
                                                     "multicraft_session": "valid-session", "unrelated": "not-forwarded"})
        response = await self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.csrf_headers = dict(self.headers, **{"X-CSRF-Token": result["csrf"]})
        return result

    async def acquire(self, session):
        socket = await self.client.ws_connect(f"/media/video/{session}", headers=self.headers)
        self.sockets.append(socket)
        await socket.receive_json()
        await socket.receive()
        response = await self.client.get("/api/state", headers=self.headers)
        state = await response.json()
        headers = dict(self.csrf_headers, **{"X-MultiCraft-Control": state["controllers"][session]})
        return socket, headers

    async def test_existing_m0_op_identity_is_scoped_and_host_token_stays_private(self):
        result = await self.login()
        self.assertEqual(result["native"]["pid"], 123)
        self.assertNotIn(self.token, json.dumps(result))
        self.assertEqual(self.auth_requests[-1], {"cookies": {"multicraft_browser": "valid-browser", "multicraft_session": "valid-session"}, "host": "expected-m0:18080"})
        self.account["isOp"] = False
        response = await self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status, 403)

    async def test_anonymous_host_origin_and_csrf_are_denied(self):
        response = await self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status, 401)
        await self.login()
        for headers in ({"Host": "evil.invalid", "Origin": "http://localhost:18092"},
                        {"Host": "localhost:18092", "Origin": "https://evil.invalid"},
                        self.headers):
            response = await self.client.post("/api/session/A/input", headers=headers, json={"type": "release"})
            self.assertEqual(response.status, 403)
        self.assertEqual(self.controls, [])

    async def test_per_session_route_and_batch_allowlist(self):
        await self.login()
        _, headers_a = await self.acquire("A")
        _, headers_b = await self.acquire("B")
        response = await self.client.post("/api/session/A/input", headers=headers_a,
                                          json=envelope([{"type": "move", "x": 17, "y": 21}, {"type": "button", "button": 1, "action": 1}]))
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/session/B/command", headers=headers_b, json={"type": "respawn", "expectedRuntimeOwner": "a" * 32, "expectedEpoch": 2})
        self.assertEqual(response.status, 200)
        for path, value, status in (("/api/session/C/input", {"type": "release"}, 404),
                                    ("/api/session/A/command", {"type": "close"}, 400),
                                    ("/api/session/A/command", {"type": "connect", "address": "evil.invalid"}, 400),
                                    ("/api/session/A/input", {"events": [{"type": "release"}, {"events": []}]}, 400)):
            response = await self.client.post(path, headers=self.csrf_headers, json=value)
            self.assertEqual(response.status, status)
        self.assertEqual([path for path, value in self.controls], ["/session/A/input", "/session/B/command"])

    async def test_media_is_session_bound_and_closing_a_keeps_b(self):
        await self.login()
        a = await self.client.ws_connect("/media/video/A", headers=self.headers)
        b = await self.client.ws_connect("/media/video/B", headers=self.headers)
        self.assertEqual((await a.receive_json())["session"], "A")
        self.assertEqual((await b.receive_json())["session"], "B")
        self.assertEqual((await a.receive()).data, b"frame-A")
        self.assertEqual((await b.receive()).data, b"frame-B")
        await a.close()
        await b.send_json({"type": "keyframe"})
        for _ in range(30):
            if self.feedback:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.feedback, [("B", '{"type": "keyframe"}')])
        self.assertFalse(b.closed)
        await b.close()

    async def test_lowered_op_cap_rejects_game_input_but_allows_key_release(self):
        await self.login()
        _, headers = await self.acquire("A")
        limits = json.loads(json.dumps(LIMITS))
        limits["fps"][1] = 30
        self.policy.write_text(json.dumps({"revision": 1, "limits": limits}))
        response = await self.client.post("/api/session/A/input", headers=headers, json=envelope([{"type": "grab"}]))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.controls, [])
        response = await self.client.post("/api/session/A/input", headers=headers, json=envelope([{"type": "release"}], 4))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.controls[0][1]["events"], [{"type": "release"}])

    async def test_abrupt_controller_close_releases_only_its_session_and_old_generation_cannot_release_new(self):
        await self.login()
        a, headers_a = await self.acquire("A")
        b, headers_b = await self.acquire("B")
        old_lease = self.gateway.controllers["A"]
        event = [{"type": "key", "scancode": 26, "keycode": 119, "action": 1}]
        for session, headers in (("A", headers_a), ("B", headers_b)):
            response = await self.client.post(f"/api/session/{session}/input", headers=headers, json=envelope(event))
            self.assertEqual(response.status, 200)
        await a.close()
        for _ in range(50):
            if "A" not in self.gateway.controllers:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.held["A"], set())
        self.assertEqual(self.held["B"], {26})
        new_a, new_headers = await self.acquire("A")
        self.assertNotEqual(new_headers["X-MultiCraft-Control"], headers_a["X-MultiCraft-Control"])
        response = await self.client.post("/api/session/A/input", headers=new_headers, json=envelope(event, 4))
        self.assertEqual(response.status, 200)
        await self.gateway.drop_controller("A", old_lease)
        self.assertEqual(self.held["A"], {26})
        response = await self.client.post("/api/session/A/input", headers=headers_a, json=envelope(event, 5))
        self.assertEqual(response.status, 409)
        self.assertEqual(self.held["A"], {26})

    async def test_delayed_command_keeps_its_epoch_and_is_rejected_by_native(self):
        await self.login()
        _, headers = await self.acquire("A")
        for command in ("disconnect", "respawn"):
            response = await self.client.post("/api/session/A/command", headers=headers,
                                              json={"type": command, "expectedRuntimeOwner": "a" * 32, "expectedEpoch": 1})
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"], "stale connection epoch")
        response = await self.client.post("/api/session/A/command", headers=headers,
                                          json={"type": "disconnect", "expectedRuntimeOwner": "a" * 32})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.controls, [])


if __name__ == "__main__":
    unittest.main()
