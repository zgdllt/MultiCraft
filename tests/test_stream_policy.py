import unittest
import json
import tempfile
from pathlib import Path

from multicraft.stream_policy import DEFAULT_CONFIG, LIMITS, StreamPolicyError, validate_config, validate_limits, load_limits, load_policy, clamp_config


class StreamPolicyTests(unittest.TestCase):
    def test_defaults_and_user_custom_resolution(self):
        self.assertEqual(validate_config(DEFAULT_CONFIG), DEFAULT_CONFIG)
        self.assertEqual(validate_config(dict(DEFAULT_CONFIG, width=1000, height=600, adaptive=False))["width"], 1000)
        self.assertEqual(validate_config(dict(DEFAULT_CONFIG, fps=1000))["fps"], 1000)

    def test_limits_types_unknown_fields_and_alignment(self):
        for change in [{"fps": 1001}, {"width": 1922}, {"height": 181}, {"bitrateKbps": 0},
                       {"adaptive": "false"}, {"fps": True}, {"fps": 30.0}, {"privateKey": "never"}]:
            with self.subTest(change=change), self.assertRaises(StreamPolicyError):
                validate_config(dict(DEFAULT_CONFIG, **change))

    def test_persistent_policy_is_reloaded_and_invalid_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "policy.json"
            self.assertEqual(load_limits(path), LIMITS)
            value = {"revision": 3, "limits": dict(LIMITS, fps=[10, 90])}
            path.write_text(json.dumps(value))
            self.assertEqual(load_policy(path), value)
            self.assertEqual(load_limits(path)["fps"], [10, 90])
            with self.assertRaises(StreamPolicyError):
                validate_config(dict(DEFAULT_CONFIG, fps=120), load_limits(path))
            path.write_text('{"revision":true,"limits":{}}')
            with self.assertRaises(StreamPolicyError):
                load_limits(path)

    def test_op_limits_have_fixed_lower_bounds_and_absolute_caps(self):
        for change in [{"width": [320, 7682]}, {"height": [180, 1081]}, {"fps": [10, 1001]},
                       {"bitrateKbps": [500, 100001]}, {"fps": [1, 60]}, {"fps": [10, True]}, {"width": [320, 318]}]:
            with self.subTest(change=change), self.assertRaises(StreamPolicyError):
                validate_limits(dict(LIMITS, **change))
        expanded = dict(LIMITS, width=[320, 7680], height=[180, 4320], bitrateKbps=[500, 100000])
        self.assertEqual(validate_limits(expanded), expanded)

    def test_clamping_preserves_adaptive_and_produces_valid_even_resolution(self):
        limits = dict(LIMITS, width=[320, 800], height=[180, 600], fps=[10, 20], bitrateKbps=[500, 1500])
        clamped = clamp_config(dict(DEFAULT_CONFIG, adaptive=False), limits)
        self.assertEqual(clamped, {"width": 800, "height": 600, "fps": 20, "bitrateKbps": 1500, "adaptive": False})
        self.assertEqual(validate_config(clamped, limits), clamped)


if __name__ == "__main__":
    unittest.main()
