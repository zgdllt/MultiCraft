import copy
import json
from pathlib import Path
import tempfile
import unittest
from multicraft.pool_config import default_config, load_config, server_address
from multicraft.m1_media import output_layout, GRID_WIDTH, GRID_HEIGHT


class PoolConfigTests(unittest.TestCase):
    def load(self, value, directory):
        path = Path(directory) / 'multicraft.json'
        path.write_text(json.dumps(value)); return load_config(path)

    def test_one_four_and_sixteen_unique_slots_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (1, 4, 16):
                cfg = self.load(default_config(count), directory)
                self.assertEqual(len(cfg['sessions']), count)
                self.assertEqual(len({entry['uuid'] for entry in cfg['sessions']}), count)
                layout = output_layout(cfg['sessions'])
                self.assertEqual(sorted(item['output'] for item in layout.values()), list(range(0, 2 * count, 2)))
                self.assertEqual(sorted(item['captureView'] for item in layout.values()), list(range(1, 2 * count, 2)))
                self.assertTrue(all(item['x'] % GRID_WIDTH == 0 and item['y'] % GRID_HEIGHT == 0 for item in layout.values()))

    def test_all_identity_and_display_collisions_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for key in ('id', 'username', 'uuid', 'displayIndex'):
                cfg = default_config(4)
                if key == 'uuid': cfg['sessions'][0][key] = cfg['sessions'][1][key] = '11111111-1111-4111-8111-111111111111'
                else: cfg['sessions'][1][key] = cfg['sessions'][0].get(key, 0)
                with self.subTest(key=key), self.assertRaises(ValueError): self.load(cfg, directory)
            cfg = default_config(4); cfg['sessions'][1]['username'] = 'mcplayer1'
            with self.assertRaises(ValueError): self.load(cfg, directory)

    def test_pool_bounds_even_geometry_and_path_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (0, 17, True):
                with self.assertRaises(ValueError): default_config(count)
            for value in ('../escape', '/tmp/escape', '.runtime'):
                cfg = default_config(); cfg['runtimeDirectory'] = value
                with self.assertRaises(ValueError): self.load(cfg, directory)
            cfg = default_config(); cfg['sessions'][0]['stream']['width'] = 3842
            with self.assertRaises(ValueError): self.load(cfg, directory)
            cfg = default_config(); cfg['sessions'][0]['stream']['fps'] = 1001
            with self.assertRaises(ValueError): self.load(cfg, directory)
            cfg = default_config(); cfg['sessions'][0]['stream']['width'] = 641
            with self.assertRaises(ValueError): self.load(cfg, directory)

    def test_explicit_server_address_has_no_test_port_pin(self):
        for address in ('localhost:25565', 'mc.example.test:25590', '[::1]:25565'):
            self.assertEqual(server_address(address), address)
        for address in ('127.0.0.1', 'x:0', 'x:65536', 'user@x:25565', 'x:25565/path', 'x:25565#x', 'x:25565?x'):
            with self.subTest(address=address), self.assertRaises(ValueError): server_address(address)

    def test_nested_symlink_runtime_alias_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / '.runtime/target').mkdir(parents=True)
            (root / '.runtime/alias').symlink_to(root / '.runtime/target', target_is_directory=True)
            cfg = default_config(); cfg['runtimeDirectory'] = '.runtime/alias/pool'
            with self.assertRaises(ValueError): self.load(cfg, directory)

    def test_persisted_op_caps_clamp_every_startup_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / '.runtime').mkdir()
            (root / '.runtime/stream-policy.json').write_text(json.dumps({'revision': 2, 'limits': {
                'width': [320, 960], 'height': [180, 540], 'fps': [10, 30], 'bitrateKbps': [500, 4000]}}))
            cfg = self.load(default_config(4), directory)
            for slot in cfg['sessions']:
                self.assertEqual(slot['stream'], {'width': 960, 'height': 540, 'fps': 30, 'bitrateKbps': 4000, 'adaptive': True})
