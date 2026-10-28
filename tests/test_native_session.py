from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_session import client_environment, expand_arguments, launch, offline_uuid, process_owned, proc_start_ticks, server_command, stop
from prepare_minecraft import download_verified, rules_allow, selected_libraries


class LauncherContractTests(unittest.TestCase):
    def test_cleanup_cannot_stop_a_replacement_launch_generation(self):
        with patch("native_session.read_record", return_value={"pid": 1, "owner": "new-generation"}), \
             patch("native_session.os.kill") as kill:
            result = stop(Path("/unused"), "client", expected_owner="old-generation")
            self.assertFalse(result["stopped"])
            kill.assert_not_called()

    def test_rules_are_ordered_and_filter_foreign_os_and_features(self):
        self.assertFalse(rules_allow([{"action": "allow", "os": {"name": "windows"}}]))
        self.assertTrue(rules_allow([{"action": "allow", "os": {"name": "linux"}}]))
        self.assertFalse(rules_allow([{"action": "allow"}, {"action": "disallow", "os": {"name": "linux"}}]))
        self.assertFalse(rules_allow([{"action": "allow", "features": {"is_demo_user": True}}]))

    def test_arguments_reject_unresolved_values(self):
        self.assertEqual(expand_arguments(["--name", "${name}", {"rules": [{"action": "allow", "os": {"name": "windows"}}], "value": "foreign"}], {"name": "M0Player"}), ["--name", "M0Player"])
        with self.assertRaises(ValueError):
            expand_arguments(["${missing}"], {})

    def test_uuid_matches_java_name_uuid_and_is_stable(self):
        self.assertEqual(offline_uuid("Notch"), "b50ad385-829d-3141-a216-7e7d7539ba7f")
        self.assertNotEqual(offline_uuid("M0Player"), offline_uuid("asta"))

    def test_borrow_is_verified_and_bad_digest_is_not_committed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jar"
            source.write_bytes(b"verified game bytes")
            destination = root / "out.jar"
            digest = hashlib.sha1(source.read_bytes()).hexdigest()
            self.assertEqual(download_verified("unused", destination, digest, borrow=source), "borrowed-verified-copy")
            self.assertEqual(download_verified("unused", destination, digest), "cached")
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            with patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.side_effect = [b"bad", b""]
                with self.assertRaises(ValueError):
                    download_verified("https://example.invalid/client", root / "bad.jar", "0" * 40)
            self.assertFalse((root / "bad.jar").exists())

    def test_library_platform_filter_does_not_include_windows(self):
        metadata = {"libraries": [
            {"name": "linux-native", "rules": [{"action": "allow", "os": {"name": "linux"}}], "downloads": {"artifact": {"path": "linux.jar"}}},
            {"name": "windows-native", "rules": [{"action": "allow", "os": {"name": "windows"}}], "downloads": {"artifact": {"path": "windows.jar"}}},
        ]}
        self.assertEqual([x["path"] for x in selected_libraries(metadata)], ["linux.jar"])

    def test_process_identity_rejects_stale_pid_and_missing_record(self):
        self.assertFalse(process_owned({"pid": 999999999, "startTicks": "1", "cwd": "/", "owner": "fake"}))
        self.assertFalse(process_owned({}))

    def test_client_never_inherits_existing_desktop_or_alias(self):
        for display in [None, "", ":1", ":1.0", "unix:1", ":0", "host:1"]:
            with self.assertRaises(ValueError):
                client_environment(display, {"DISPLAY": ":1"})
        self.assertEqual(client_environment(":96", {"DISPLAY": ":1"})["DISPLAY"], ":96")

    def test_wayland_requires_socket_and_clears_inherited_x11(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"DISPLAY": ":1", "WAYLAND_DISPLAY": "m0-wayland", "XDG_RUNTIME_DIR": directory,
                   "PULSE_SERVER": "unix:private-audio"}
            with self.assertRaises(ValueError):
                client_environment(None, env)
            with socket.socket(socket.AF_UNIX) as compositor:
                compositor.bind(str(Path(directory) / "m0-wayland"))
                actual = client_environment(None, env)
                self.assertNotIn("DISPLAY", actual)
                self.assertEqual(actual["PULSE_SERVER"], env["PULSE_SERVER"])

    def test_console_rejects_unicode_payload_above_atomic_pipe_limit(self):
        with patch("native_session.read_record", return_value={"pid": 1}), \
             patch("native_session.process_owned", return_value=True), \
             patch("native_session.os.open") as opening:
            with self.assertRaises(ValueError):
                server_command(Path("/unused"), "中" * 1400)
            opening.assert_not_called()

    def test_running_client_rejects_different_configuration_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("native_session.read_record", return_value={"pid": 1, "launchFingerprint": "other"}), \
                 patch("native_session.process_owned", return_value=True), \
                 patch("native_session.subprocess.Popen") as spawning:
                with self.assertRaisesRegex(ValueError, "another launch configuration"):
                    launch(Path(directory), "client", "/java", ["--username", "changed"], Path(directory))
                spawning.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "ownership uses Linux /proc")
    def test_process_identity_requires_start_time_cwd_and_exact_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)",
                                      "-Dmulticraft.owner=test-owned"], cwd=directory)
            try:
                record = {"pid": child.pid, "startTicks": proc_start_ticks(child.pid),
                          "cwd": directory, "owner": "test-owned"}
                self.assertTrue(process_owned(record))
                for replacement in [{"startTicks": "0"}, {"cwd": "/"}, {"owner": "wrong"}]:
                    self.assertFalse(process_owned({**record, **replacement}))
            finally:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
