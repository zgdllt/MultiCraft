import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from multicraft.media_runtime import (DEFAULT, MediaAdapter, configure_selkies,
                                      private_environment, start_audio, validate_config,
                                      TaskReentrantLock)


class ConfigTests(unittest.TestCase):
    def test_rejects_booleans_nonfinite_dimensions_and_unbounded_values(self):
        for change in ({"width": True}, {"fps": float("nan")}, {"height": 181},
                       {"width": 2000}, {"bitrateKbps": 100}, {"adaptive": "false"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config({**DEFAULT, **change})
        self.assertEqual(validate_config(DEFAULT), DEFAULT)
        self.assertEqual(validate_config({**DEFAULT, "fps": 1000})["fps"], 1000)
        with self.assertRaises(ValueError):
            validate_config({**DEFAULT, "fps": 1001})

    def test_private_environment_does_not_inherit_desktop_or_audio(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "DISPLAY": ":1", "WAYLAND_SOCKET": "5", "PULSE_SERVER": "unix:/host/pulse",
            "PIPEWIRE_REMOTE": "host", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/bus",
        }):
            xdg = private_environment(Path(folder))
            self.assertNotIn("DISPLAY", os.environ)
            self.assertNotIn("WAYLAND_SOCKET", os.environ)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", os.environ)
            self.assertNotIn("PIPEWIRE_REMOTE", os.environ)
            self.assertTrue(os.environ["PULSE_SERVER"].startswith("unix:" + str(xdg)))
            self.assertEqual(xdg.stat().st_mode & 0o777, 0o700)

    def test_empty_master_token_cannot_enable_open_media(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            token = Path(folder) / "token"
            token.write_text("")
            token.chmod(0o600)
            args = SimpleNamespace(port=18081, render_node="/dev/dri/renderD128", master_token_file=token)
            with self.assertRaises(ValueError):
                configure_selkies(args, DEFAULT)

    def test_host_capture_and_command_environment_are_removed(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "SELKIES_WAYLAND_HOST_DISPLAY": "host-desktop", "SELKIES_RUN_AFTER_CONNECT": "bad",
            "PIXELFLUX_CU": "0.0.0.0:1234", "PCMFLUX_DEVICE": "host.monitor",
        }):
            token = Path(folder) / "token"
            token.write_text("x" * 40)
            token.chmod(0o600)
            args = SimpleNamespace(port=18081, render_node="/dev/dri/renderD128", master_token_file=token)
            configure_selkies(args, DEFAULT)
            self.assertEqual(os.environ["SELKIES_WAYLAND_HOST_DISPLAY"], "")
            self.assertNotIn("SELKIES_RUN_AFTER_CONNECT", os.environ)
            self.assertNotIn("PIXELFLUX_CU", os.environ)
            self.assertNotIn("PCMFLUX_DEVICE", os.environ)
            self.assertEqual(os.environ["SELKIES_COMMAND_ENABLED"], "false")
            self.assertEqual(os.environ["SELKIES_SCALING_DPI"], "96")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_transaction_blocks_old_reconfigure_and_direct_stale_start_until_new_capture_is_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, service, capture = self.adapter(folder)
            state = service.display_clients["primary"]
            state["video_active"] = True
            old_entered, release_old = asyncio.Event(), asyncio.Event()
            stopped, proceed = asyncio.Event(), asyncio.Event()
            stale_started = asyncio.Event()
            events = []

            async def old_reconfigure():
                async with service._reconfigure_lock:
                    async with service._video_capture_lock:
                        old_entered.set()
                        await release_old.wait()
                        events.append("old pass finished")

            async def stop(_):
                async with service._video_capture_lock:
                    capture.module.is_capturing = False
                    stopped.set()
                    events.append("old capture stopped")
                    await proceed.wait()

            async def reconfigure():
                # Match the pinned upstream's coalescing behavior.
                if service._reconfigure_lock.locked():
                    service._reconfigure_pending = True
                    return
                raise AssertionError("reconfigure escaped the transaction")

            async def apply_settings(_, values, *__):
                async with service._reconfigure_lock:
                    state.update(width=values["manual_width"], height=values["manual_height"])
                await reconfigure()

            async def rebuild():
                async with service._video_capture_lock:
                    capture.capture_width, capture.capture_height = state["width"], state["height"]
                    capture.module.is_capturing = True
                    events.append("new capture ready")

            async def stale_start():
                # START_VIDEO bypasses R and waits only on V. Its old-size
                # request must see a live new capture, as upstream then returns.
                async with service._video_capture_lock:
                    stale_started.set()
                    self.assertTrue(capture.module.is_capturing)
                    self.assertEqual((capture.capture_width, capture.capture_height), (960, 540))
                    events.append("stale start kept new capture")

            capture.module = service.capture_instances["primary"]["module"]
            service._stop_capture_for_display.side_effect = stop
            service._apply_client_settings.side_effect = apply_settings
            service.reconfigure_displays.side_effect = reconfigure
            service._reconfigure_displays_locked = rebuild
            old_task = asyncio.create_task(old_reconfigure())
            await old_entered.wait()
            apply_task = asyncio.create_task(adapter.apply({**DEFAULT, "width": 960, "height": 540}))
            await asyncio.sleep(0)
            self.assertFalse(stopped.is_set())
            release_old.set()
            await asyncio.wait_for(stopped.wait(), 1)
            stale_task = asyncio.create_task(stale_start())
            await asyncio.sleep(0)
            self.assertFalse(stale_started.is_set())
            proceed.set()
            await asyncio.wait_for(asyncio.gather(old_task, apply_task, stale_task), 1)
            self.assertEqual(events, ["old pass finished", "old capture stopped", "new capture ready",
                                      "stale start kept new capture"])
            self.assertEqual(adapter.config["width"], 960)

    async def test_reentrant_lock_does_not_share_ownership_with_child_tasks_and_survives_cancellation(self):
        lock = TaskReentrantLock()
        async with lock:
            async with lock:
                waiter = asyncio.create_task(lock.acquire())
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())
        with self.assertRaisesRegex(RuntimeError, "abort"):
            async with lock:
                raise RuntimeError("abort")
        self.assertFalse(lock.locked())

    async def test_media_reloads_the_same_operator_policy_before_applying(self):
        from multicraft.stream_policy import LIMITS
        with tempfile.TemporaryDirectory() as folder:
            adapter, service, _ = self.adapter(folder)
            adapter.policy_file.parent.mkdir(parents=True)
            limits = {**LIMITS, "fps": [10, 20]}
            adapter.policy_file.write_text(json.dumps({"revision": 1, "limits": limits}))
            with self.assertRaises(ValueError):
                await adapter.apply(DEFAULT)
            service._apply_client_settings.assert_not_awaited()

    def adapter(self, folder):
        capture = SimpleNamespace(capture_width=1280, capture_height=720,
                                  target_fps=30, video_bitrate_kbps=6000)
        state = {"ws": object(), "link_kbps": 1000, "video_bitrate": 6000, "framerate": 30,
                 "width": 1280, "height": 720}
        service = SimpleNamespace(
            app=SimpleNamespace(set_framerate=lambda fps: None),
            display_clients={"primary": state}, clients=set(), is_pcmflux_capturing=True,
            capture_instances={"primary": {"settings": capture, "module": SimpleNamespace(is_capturing=True)}},
            _apply_client_settings=AsyncMock(), _handle_opcode_fps=AsyncMock(),
            _stop_capture_for_display=AsyncMock(),
            reconfigure_displays=AsyncMock(),
            _handle_opcode_video_bitrate=AsyncMock(), _tell_display_settings=AsyncMock(),
            _broadcast_live_server_settings=AsyncMock(),
        )
        adapter = MediaAdapter(SimpleNamespace(services={"websockets": service}), SimpleNamespace(), Path(folder))
        return adapter, service, capture

    async def test_disabling_adaptation_clears_old_limit_and_keeps_a_real_floor(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, service, _ = self.adapter(folder)
            await adapter.apply({**DEFAULT, "adaptive": False})
            self.assertNotIn("link_kbps", service.display_clients["primary"])
            self.assertEqual(adapter.settings.video_bitrate, (500, 6000))
            self.assertFalse(adapter.settings.congestion_control[0])

    async def test_resize_does_not_mutate_display_if_old_capture_cannot_release(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, service, _ = self.adapter(folder)
            service._stop_capture_for_display.side_effect = RuntimeError("cannot release capture")
            with self.assertRaisesRegex(RuntimeError, "cannot release capture"):
                await adapter.apply({**DEFAULT, "width": 960, "height": 540})
            service._apply_client_settings.assert_not_awaited()
            self.assertEqual(adapter.config, DEFAULT)

    async def test_failed_capture_restart_is_not_acknowledged_and_rollback_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, service, _ = self.adapter(folder)
            service.display_clients["primary"]["video_active"] = True
            module = service.capture_instances["primary"]["module"]
            module.is_capturing = False
            with self.assertRaisesRegex(RuntimeError, "did not recover"):
                await adapter.apply(DEFAULT)
            service.reconfigure_displays.assert_awaited_once()
            # Even unchanged settings must explicitly retry the missing capture.
            async def recover():
                module.is_capturing = True
            service.reconfigure_displays.side_effect = recover
            await adapter.apply(DEFAULT)
            self.assertTrue(adapter.observed()["ready"])

    async def test_native_rate_update_failure_is_not_acknowledged_as_effective(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, _, _ = self.adapter(folder)
            with self.assertRaisesRegex(RuntimeError, "frame rate"):
                await adapter.apply({**DEFAULT, "fps": 60})
            self.assertEqual(adapter.config, DEFAULT)

    async def test_known_decoder_pace_is_reported_separately_from_the_request(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, service, capture = self.adapter(folder)
            service.display_clients["primary"].update(framerate=1000, decode_pace=60)
            capture.target_fps = 60
            await adapter.apply({**DEFAULT, "fps": 1000})
            self.assertEqual(adapter.config["fps"], 1000)
            self.assertEqual(adapter.observed()["captureFpsTarget"], 60)

    async def test_adaptive_target_update_failure_is_not_acknowledged(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, _, _ = self.adapter(folder)
            with self.assertRaisesRegex(RuntimeError, "adaptive bitrate target"):
                await adapter.apply({**DEFAULT, "bitrateKbps": 10000, "adaptive": True})
            self.assertEqual(adapter.config, DEFAULT)

    async def test_rollback_failure_emits_fatal_ack_and_is_observable(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, _, _ = self.adapter(folder)
            adapter.apply = AsyncMock(side_effect=[RuntimeError("bad change"), RuntimeError("bad rollback")])
            request = Path(folder) / "request.json"
            effective = Path(folder) / "effective.json"
            request.write_text(json.dumps({"requestId": "test", "generation": 1, "config": DEFAULT}))
            with self.assertRaisesRegex(RuntimeError, "rollback both failed"):
                await adapter.watch(request, effective)
            ack = json.loads(effective.read_text())
            self.assertTrue(ack["fatal"])
            self.assertIsNone(ack["effective"])

    async def test_partial_audio_start_failure_reaps_its_children(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "logs").mkdir()
            child = SimpleNamespace(returncode=None, terminate=lambda: None, wait=AsyncMock(return_value=0))
            with patch("multicraft.media_runtime.asyncio.create_subprocess_exec",
                       AsyncMock(side_effect=[child, FileNotFoundError("wireplumber")])):
                with self.assertRaises(FileNotFoundError):
                    await start_audio(root)
            child.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
