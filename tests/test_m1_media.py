from pathlib import Path
import tempfile
import unittest

from multicraft.m1_media import assignments, private_environment


class M1CompositorContractTests(unittest.TestCase):
    def test_environment_does_not_reuse_m0_or_host_sockets(self):
        with tempfile.TemporaryDirectory() as directory:
            env = private_environment(Path(directory), {"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-m0",
                "PULSE_SERVER": "unix:/host/sound", "PIPEWIRE_REMOTE": "host", "PULSE_SINK": "host",
                "DBUS_SESSION_BUS_ADDRESS": "host", "SELKIES_FOO": "host", "PATH": "/usr/bin"})
            self.assertNotIn("DISPLAY", env)
            self.assertNotIn("WAYLAND_DISPLAY", env)
            self.assertNotIn("PIPEWIRE_REMOTE", env)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
            self.assertNotIn("SELKIES_FOO", env)
            self.assertEqual(env["PULSE_SERVER"], "unix:" + directory + "/xdg/pulse/native")
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual((Path(directory) / "xdg").stat().st_mode & 0o777, 0o700)

    def test_only_unique_exact_session_titles_are_bound(self):
        self.assertEqual(assignments([(11, "MultiCraft M1 A", "mc", 1, False),
            (12, "MultiCraft M1 B", "mc", 0, False), (13, "Minecraft", "mc", 0, False)]), [(11, 0), (12, 2)])
        self.assertEqual(assignments([(11, "MultiCraft M1 A", "mc", 1, False),
            (12, "MultiCraft M1 A", "mc", 1, False)]), [])
        self.assertEqual(assignments([(11, "MultiCraft M1 A", "mc", 0, False)]), [])
        self.assertEqual(assignments([(11, "MultiCraft M1 A", "mc", 0, True)]), [(11, 0)])


if __name__ == "__main__":
    unittest.main()
