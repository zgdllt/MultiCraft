"""Gateway contracts; media/native below are fakes, not gameplay evidence."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from multicraft.release_gateway import ReleaseGateway
from multicraft.stream_policy import DEFAULT_CONFIG, LIMITS, load_policy


class ReleaseGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        self.public = {"format": "spki", "key": base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()}
        self.slots = ["alpha", "bravo", "charlie", "delta"]
        self.accounts = [{"id": f"user{i}", "linuxUser": "asta" if i == 0 else f"player{i}", "nativeSession": slot, "minecraftUsername": f"Player{i}", "publicKeys": [self.public]} for i, slot in enumerate(self.slots)]
        self.registry = self.root / "accounts.json"
        self.save_registry()
        token = self.root / "token"
        token.write_text("x" * 48)
        token.chmod(0o600)
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"revision": 0, "limits": LIMITS}))
        self.controls, self.feedback, self.fail_slot = [], [], None
        self.native_owner = "a" * 32
        self.native_slots = {slot: {"id": slot, "connectionEpoch": 2, "lastInputSeq": 0} for slot in self.slots}
        self.media_states = {slot: {"generation": 0, "effective": copy.deepcopy(DEFAULT_CONFIG)} for slot in self.slots}

        async def native(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + "x" * 48)
            if request.path == "/state":
                return web.json_response({"runtimeOwner": self.native_owner, "sessions": list(self.native_slots.values())})
            slot, action, value = request.match_info["slot"], request.match_info["action"], await request.json()
            current = self.native_slots[slot]
            if value.get("expectedRuntimeOwner") != self.native_owner or value.get("expectedEpoch") != current["connectionEpoch"]:
                return web.json_response({"error": "stale native generation"}, status=400)
            self.controls.append((slot, action, value))
            if action == "command" and value["type"] in {"connect", "disconnect"}:
                current["connectionEpoch"] += 1
                current["lastInputSeq"] = 0
            elif action == "input":
                if value["inputSeq"] <= current["lastInputSeq"]:
                    return web.json_response({"error": "stale sequence"}, status=400)
                current["lastInputSeq"] = value["inputSeq"]
            return web.json_response({"ok": True, **current})
        native_app = web.Application()
        native_app.router.add_get("/state", native)
        native_app.router.add_post("/session/{slot}/{action}", native)
        self.native = TestServer(native_app)
        await self.native.start_server()

        async def media_status(_):
            return web.json_response({"sessions": self.media_states})
        async def media_config(request):
            slot = request.match_info["slot"]
            value = await request.json()
            current = self.media_states[slot]
            if value["expectedGeneration"] != current["generation"]:
                return web.json_response({"error": "stale generation"}, status=409)
            if self.fail_slot == slot:
                self.fail_slot = None
                return web.json_response({"error": "mock apply failure", "applied": False}, status=503)
            current["generation"] += 1
            current["effective"] = value["config"]
            return web.json_response({"applied": True, "requestId": value["requestId"], **current})
        async def media_stream(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            slot = request.match_info["slot"]
            await ws.send_json({"type": request.match_info["kind"], "session": slot, "generation": 0})
            async for message in ws:
                self.feedback.append((slot, message.data))
            return ws
        media_app = web.Application()
        media_app.router.add_get("/status", media_status)
        media_app.router.add_post("/session/{slot}/config", media_config)
        media_app.router.add_get("/{kind:video|audio}/{slot}", media_stream)
        self.media_runner = web.AppRunner(media_app)
        await self.media_runner.setup()
        socket = self.root / "media.sock"
        await web.UnixSite(self.media_runner, str(socket)).start()
        self.origin = "http://127.0.0.1:18443"
        self.gateway = ReleaseGateway(origin=self.origin, accounts_file=self.registry, token_file=token, media_socket=socket,
            policy_file=self.policy, sessions=self.slots, native_url=str(self.native.make_url("")).rstrip("/"), insecure_loopback=True)
        self.client = TestClient(TestServer(self.gateway.app), cookie_jar=CookieJar(unsafe=True), headers={"Host": "127.0.0.1:18443"})
        await self.client.start_server()
        self.sockets = []

    def save_registry(self):
        self.registry.write_text(json.dumps({"operatorAccountId": "user0", "accounts": self.accounts}))

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        await self.client.close()
        await self.media_runner.cleanup()
        await self.native.close()
        self.temp.cleanup()

    async def call(self, path, value=None, *, method=None, csrf=None, generation=None):
        headers = {"Origin": self.origin}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if generation:
            headers["X-MultiCraft-Control"] = generation
        response = await self.client.request(method or ("GET" if value is None else "POST"), path, json=value, headers=headers)
        return response, await response.json()

    async def login(self, index):
        response = await self.client.get("/", allow_redirects=False)
        prefix = response.headers["Location"]
        response, bootstrap = await self.call(prefix + "api/bootstrap")
        cookie = response.cookies["multicraft_release_browser"]
        self.assertEqual(cookie["path"], prefix)
        self.assertTrue(cookie["httponly"])
        csrf = bootstrap["csrf"]
        _, challenge = await self.call(prefix + "api/auth/challenge", {"publicKey": self.public}, csrf=csrf)
        signature = base64.b64encode(self.key.sign(base64.b64decode(challenge["challengeBase64"]))).decode()
        _, verified = await self.call(prefix + "api/auth/verify", {"attemptId": challenge["attemptId"], "signature": signature}, csrf=csrf)
        response, selected = await self.call(prefix + "api/auth/select", {"preAuthId": verified["preAuthId"], "accountId": f"user{index}"}, csrf=csrf)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.cookies["multicraft_release_session"]["path"], prefix)
        self.assertNotIn("sessionToken", selected)
        return prefix, csrf

    async def video(self, prefix):
        ws = await self.client.ws_connect(prefix + "media/video", headers={"Origin": self.origin})
        self.sockets.append(ws)
        control = await ws.receive_json()
        self.assertEqual(control["type"], "control")
        metadata = await ws.receive_json()
        return ws, control["generation"], metadata

    async def test_four_paths_keep_four_identities_and_logout_is_local(self):
        tabs = [await self.login(i) for i in range(4)]
        for i, (prefix, _) in enumerate(tabs):
            response, state = await self.call(prefix + "api/state")
            self.assertEqual(response.status, 200)
            self.assertEqual(state["account"]["id"], f"user{i}")
            self.assertEqual(state["native"]["id"], self.slots[i])
            self.assertNotIn("sessions", state)
            self.assertNotIn("token", json.dumps(state))
        prefix, csrf = tabs[1]
        response, _ = await self.call(prefix + "api/logout", {}, csrf=csrf)
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.call(prefix + "api/me"))[0].status, 401)
        for i in (0, 2, 3):
            self.assertEqual((await self.call(tabs[i][0] + "api/me"))[1]["account"]["id"], f"user{i}")

    async def test_ws_and_input_are_bound_to_account_not_user_selected_slot(self):
        alpha, bravo = await self.login(0), await self.login(1)
        wa, ga, ma = await self.video(alpha[0])
        wb, gb, mb = await self.video(bravo[0])
        self.assertEqual(ma["session"], "alpha")
        self.assertEqual(mb["session"], "bravo")
        value = {"expectedRuntimeOwner": "a" * 32, "expectedEpoch": 2, "inputSeq": 1, "events": [{"type": "text", "text": "中文"}]}
        response, _ = await self.call(bravo[0] + "api/session/input", value, csrf=bravo[1], generation=ga)
        self.assertEqual(response.status, 409)
        response, _ = await self.call(bravo[0] + "api/session/input", value, csrf=bravo[1], generation=gb)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.controls[-1][0], "bravo")
        await wa.close()
        for _ in range(30):
            if "alpha" not in self.gateway.controllers:
                break
            await asyncio.sleep(.01)
        self.assertNotIn("alpha", self.gateway.controllers)
        self.assertIn("bravo", self.gateway.controllers)
        response, _ = await self.call(bravo[0] + "api/session/input", {**value, "inputSeq": 2}, csrf=bravo[1], generation=gb)
        self.assertEqual(response.status, 200)
        self.assertFalse(wb.closed)

    async def test_op_lowering_is_all_streams_or_rollback_before_revision_commit(self):
        prefix, csrf = await self.login(0)
        limits = copy.deepcopy(LIMITS)
        limits["bitrateKbps"][1] = 4000
        self.fail_slot = "bravo"
        response, _ = await self.call(prefix + "api/op/stream-limits", {"limits": limits, "expectedRevision": 0}, method="PUT", csrf=csrf)
        self.assertEqual(response.status, 409)
        self.assertEqual(load_policy(self.policy)["revision"], 0)
        self.assertTrue(all(value["effective"] == DEFAULT_CONFIG for value in self.media_states.values()))
        response, result = await self.call(prefix + "api/op/stream-limits", {"limits": limits, "expectedRevision": 0}, method="PUT", csrf=csrf)
        self.assertEqual(response.status, 200)
        self.assertEqual(result["revision"], 1)
        self.assertTrue(all(value["effective"]["bitrateKbps"] == 4000 for value in self.media_states.values()))
        response, _ = await self.call(prefix + "api/op/stream-limits", {"limits": limits, "expectedRevision": 0}, method="PUT", csrf=csrf)
        self.assertEqual(response.status, 409)

    async def test_policy_and_input_reject_csrf_origin_host_and_key_revocation(self):
        prefix, csrf = await self.login(1)
        response, _ = await self.call(prefix + "api/op/stream-limits")
        self.assertEqual(response.status, 403)
        response, _ = await self.call(prefix + "api/stream-config", DEFAULT_CONFIG)
        self.assertEqual(response.status, 401)
        response = await self.client.post(prefix + "api/logout", json={}, headers={"Origin": "https://untrusted.example", "X-CSRF-Token": csrf})
        self.assertEqual(response.status, 403)
        response = await self.client.get(prefix + "api/me", headers={"Host": "untrusted.example"})
        self.assertEqual(response.status, 403)
        self.accounts[1]["publicKeys"] = []
        self.save_registry()
        self.assertEqual((await self.call(prefix + "api/state"))[0].status, 401)

    async def test_own_config_is_acked_and_caps_reject_changes_before_media(self):
        prefix, csrf = await self.login(2)
        config = {**DEFAULT_CONFIG, "fps": 120, "bitrateKbps": 10000, "adaptive": False}
        response, data = await self.call(prefix + "api/stream-config", config, csrf=csrf)
        self.assertEqual(response.status, 200)
        self.assertEqual(data["effective"], config)
        self.assertEqual(self.media_states["charlie"]["effective"], config)
        self.assertEqual(self.media_states["alpha"]["effective"], DEFAULT_CONFIG)
        response, _ = await self.call(prefix + "api/stream-config", {**config, "fps": 1001}, csrf=csrf)
        self.assertEqual(response.status, 400)

    async def test_queued_input_rechecks_revocation_after_waiting_for_lock(self):
        prefix, csrf = await self.login(1)
        _, generation, _ = await self.video(prefix)
        lock, waiting = asyncio.Lock(), asyncio.Event()
        await lock.acquire()
        class ObservedLock:
            async def __aenter__(_):
                waiting.set()
                await lock.acquire()
            async def __aexit__(_, *args):
                lock.release()
        self.gateway.input_locks["bravo"] = ObservedLock()
        value = {"expectedRuntimeOwner": "a" * 32, "expectedEpoch": 2, "inputSeq": 1,
                 "events": [{"type": "text", "text": "must not reach native"}]}
        pending = asyncio.create_task(self.call(prefix + "api/session/input", value, csrf=csrf, generation=generation))
        await asyncio.wait_for(waiting.wait(), 2)
        self.accounts[1]["enabled"] = False
        self.save_registry()
        lock.release()
        response, _ = await pending
        self.assertEqual(response.status, 401)
        self.assertFalse(any(action == "input" and "events" in data for _, action, data in self.controls))

    async def test_operator_cannot_advertise_limits_beyond_native_backend(self):
        prefix, csrf = await self.login(0)
        limits = copy.deepcopy(LIMITS)
        limits["width"][1] = 7680
        response, data = await self.call(prefix + "api/op/stream-limits",
            {"limits": limits, "expectedRevision": 0}, method="PUT", csrf=csrf)
        self.assertEqual(response.status, 400)
        self.assertIn("3840", data["error"])
        self.assertEqual(load_policy(self.policy)["revision"], 0)
        self.assertTrue(all(value["generation"] == 0 for value in self.media_states.values()))

    async def test_legacy_root_cookies_do_not_override_release_tab_login(self):
        from yarl import URL
        self.client.session.cookie_jar.update_cookies(
            {"multicraft_browser": "legacy-browser", "multicraft_session": "legacy-session"},
            response_url=URL(str(self.client.make_url("/"))))
        prefix, csrf = await self.login(0)
        response, state = await self.call(prefix + "api/state")
        self.assertEqual(response.status, 200)
        self.assertEqual(state["account"]["id"], "user0")
        await self.call(prefix + "api/logout", {}, csrf=csrf)
        legacy = self.client.session.cookie_jar.filter_cookies(URL(str(self.client.make_url("/"))))
        self.assertEqual(legacy["multicraft_browser"].value, "legacy-browser")
        self.assertEqual(legacy["multicraft_session"].value, "legacy-session")

    async def test_cookies_cannot_authenticate_a_different_tab_namespace(self):
        prefix, csrf = await self.login(0)
        other, _ = await self.login(1)
        browser = self.gateway.tabs[prefix.split('/')[2]]["browser"]
        token = next(key for key, value in self.gateway.auth.sessions.items() if value["browser"] == browser)
        self.client.session.cookie_jar.clear()
        response = await self.client.get(other + "api/me", headers={"Cookie": f"multicraft_release_browser={browser}; multicraft_release_session={token}"})
        self.assertEqual(response.status, 401)


    async def test_cleanup_releases_only_its_saved_native_owner_and_epoch(self):
        prefix, _ = await self.login(0)
        ws, _, _ = await self.video(prefix)
        lease = self.gateway.controllers["alpha"]
        self.native_owner = "b" * 32
        await self.gateway.drop_controller("alpha", lease)
        self.assertNotIn("alpha", self.gateway.controllers)
        self.assertFalse(self.controls)
        await ws.close()
        # A new lease belongs to the new host, but an external network epoch
        # change still cannot be adopted by that old lease's cleanup.
        ws, _, _ = await self.video(prefix)
        lease = self.gateway.controllers["alpha"]
        self.native_slots["alpha"]["connectionEpoch"] += 1
        await self.gateway.drop_controller("alpha", lease)
        self.assertFalse(self.controls)
        await ws.close()

    async def test_cleanup_uses_latest_input_seq_and_successful_own_command_epoch(self):
        prefix, csrf = await self.login(1)
        _, generation, _ = await self.video(prefix)
        value = {"expectedRuntimeOwner": self.native_owner, "expectedEpoch": 2, "inputSeq": 15,
                 "events": [{"type": "grab"}]}
        response, _ = await self.call(prefix + "api/session/input", value, csrf=csrf, generation=generation)
        self.assertEqual(response.status, 200)
        response, _ = await self.call(prefix + "api/session/command", {
            "type": "disconnect", "expectedRuntimeOwner": self.native_owner, "expectedEpoch": 2}, csrf=csrf, generation=generation)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.gateway.controllers["bravo"]["nativeEpoch"], 3)
        response, _ = await self.call(prefix + "api/session/input", {**value, "expectedEpoch": 3, "inputSeq": 9}, csrf=csrf, generation=generation)
        self.assertEqual(response.status, 200)
        await self.gateway.drop_controller("bravo", self.gateway.controllers["bravo"])
        slot, action, released = self.controls[-1]
        self.assertEqual((slot, action), ("bravo", "input"))
        self.assertEqual(released, {"expectedRuntimeOwner": self.native_owner, "expectedEpoch": 3,
                                   "inputSeq": 10, "events": [{"type": "release"}]})

    async def test_native_generation_changes_between_cleanup_read_and_post_are_fenced(self):
        prefix, _ = await self.login(2)
        _, _, _ = await self.video(prefix)
        original = self.gateway.native
        async def racing_native(method, path, value=None):
            if method == "POST" and value and value.get("events") == [{"type": "release"}]:
                self.native_owner = "c" * 32
            return await original(method, path, value)
        self.gateway.native = racing_native
        with self.assertRaises(web.HTTPConflict):
            await self.gateway.drop_controller("charlie", self.gateway.controllers["charlie"])
        self.assertFalse(self.controls)
        self.assertNotIn("charlie", self.gateway.controllers)


    async def test_media_config_requires_matching_request_and_generation_ack(self):
        original = self.gateway.media_request
        async def wrong_ack(method, path, value=None):
            result = await original(method, path, value)
            if method == "POST":
                result["requestId"] = "another-request"
            return result
        self.gateway.media_request = wrong_ack
        with self.assertRaises(web.HTTPBadGateway):
            await self.gateway.apply_config("alpha", DEFAULT_CONFIG, self.media_states["alpha"].copy())
