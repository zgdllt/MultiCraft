from pathlib import Path
import json
import os
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from m1_session import host_environment, pin_graphics_options, prepare, validate_port


class M1LauncherBoundaryTests(unittest.TestCase):
    @staticmethod
    def cache_fixture(directory):
        cache = directory / "cache"
        cache.mkdir()
        (cache / "client.jar").write_bytes(b"cache fixture")
        (cache / "server.jar").write_bytes(b"cache fixture")
        (cache / "version.json").write_text(json.dumps({"arguments": {"game": [
            "--username", "${auth_player_name}", "--gameDir", "${game_directory}",
            "--assetsDir", "${assets_root}"]}}))
        (cache / "lock.json").write_text(json.dumps({"libraryPaths": [], "assetIndex": "fixture"}))
        return cache

    def test_generated_profile_isolated_directories_and_validated_runtime_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "m1"
            root.mkdir()
            cache = self.cache_fixture(base)
            prepare(root, cache, 25586, 18091, 1280, 720)
            profile = json.loads((root / "host.json").read_text())
            self.assertEqual(profile["audioMode"], "loopback-pcm")
            self.assertEqual(profile["teardownMode"], "defer-native")
            self.assertEqual(profile["sdlQuitPolicy"], "process-exit")
            self.assertEqual([session["displayIndex"] for session in profile["sessions"]], [0, 1])
            games = []
            for session in profile["sessions"]:
                args = session["args"]
                games.append(args[args.index("--gameDir") + 1])
                self.assertIn("--fullscreen", args)
                self.assertEqual(args[args.index("--graphicsBackend") + 1], "vulkan")
                self.assertEqual(args[args.index("--assetsDir") + 1], str(cache / "assets"))
            self.assertNotEqual(*games)
            self.assertTrue(all(Path(game).is_relative_to(root / "sessions") for game in games))
            self.assertFalse((cache / "options.txt").exists())

    def test_control_token_cannot_be_a_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "m1"
            root.mkdir()
            cache = self.cache_fixture(base)
            os.mkfifo(root / "control.token", 0o600)
            with self.assertRaisesRegex(ValueError, "regular file"):
                prepare(root, cache, 25586, 18091, 1280, 720)

    def test_graphics_backend_option_uses_native_json_string_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory)
            path = game / "options.txt"
            path.write_text("fullscreen:true\npreferredGraphicsBackend:default\n")
            pin_graphics_options(game)
            pin_graphics_options(game)
            self.assertEqual(path.read_text(), 'fullscreen:true\npreferredGraphicsBackend:"vulkan"\ninactivityFpsLimit:"minimized"\nmaxFps:60\n')

    def test_private_environment_clears_inherited_endpoints_and_rejects_external_sockets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "m1"
            runtime = root / "media" / "xdg"
            (runtime / "pulse").mkdir(parents=True)
            with socket.socket(socket.AF_UNIX) as wayland, socket.socket(socket.AF_UNIX) as pulse:
                wayland.bind(str(runtime / "wayland-1"))
                pulse.bind(str(runtime / "pulse" / "native"))
                values = {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-1",
                          "PULSE_SERVER": "unix:" + str(runtime / "pulse" / "native")}
                inherited = {"PATH": "/usr/bin", "WAYLAND_SOCKET": "42", "DISPLAY": ":1",
                             "PULSE_RUNTIME_PATH": "/outside", "PIPEWIRE_RUNTIME_DIR": "/outside",
                             "ALSOFT_CONF": "/outside", "SELKIES_FAKE": "external"}
                actual = host_environment(root, {"environment": values, "audioSinks": {}}, inherited)
                self.assertEqual(actual["WAYLAND_DISPLAY"], "wayland-1")
                self.assertEqual(actual["PATH"], "/usr/bin")
                for key in inherited.keys() - {"PATH"}:
                    self.assertNotIn(key, actual)
                with self.assertRaises(ValueError):
                    host_environment(root, {**values, "WAYLAND_DISPLAY": str(runtime / "wayland-1")}, {})
                with self.assertRaises(ValueError):
                    host_environment(root, {**values, "PULSE_SERVER": "unix:/outside/native"}, {})
                with self.assertRaises(ValueError):
                    host_environment(root, {**values, "WAYLAND_SOCKET": "42"}, {})

    def test_wayland_symlink_cannot_escape_the_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "m1"
            runtime = root / "media" / "xdg"
            (runtime / "pulse").mkdir(parents=True)
            with socket.socket(socket.AF_UNIX) as outside, socket.socket(socket.AF_UNIX) as pulse:
                outside.bind(str(Path(directory) / "outside"))
                pulse.bind(str(runtime / "pulse" / "native"))
                (runtime / "wayland-1").symlink_to(Path(directory) / "outside")
                with self.assertRaises(ValueError):
                    host_environment(root, {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-1",
                                           "PULSE_SERVER": "unix:" + str(runtime / "pulse" / "native")}, {})

    def test_ports_cannot_overlap_production_or_accepted_baseline(self):
        for port in (25565, 25575, 25585, 18080, 18081, 8795, 22, 65536):
            with self.assertRaises(ValueError):
                validate_port(port)
        self.assertEqual(validate_port(25586), 25586)


if __name__ == "__main__":
    unittest.main()
