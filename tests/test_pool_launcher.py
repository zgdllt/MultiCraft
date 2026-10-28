import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location("pool_launcher", Path(__file__).resolve().parents[1] / "scripts/multicraft.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
media_ready = launcher.media_ready


class MediaReadinessTests(unittest.TestCase):
    def test_only_current_ready_owner_and_worker_can_release_host(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory); media = runtime / 'media'; media.mkdir()
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(media / 'control.sock'))
                record = {'owner': 'current', 'startedAt': 100}
                def write(owner='current', ready=True, stamp=101, worker=20, envworker=20):
                    (media / 'state.json').write_text(json.dumps({'owner': owner, 'pid': worker, 'ready': ready, 'updatedAt': stamp}))
                    (media / 'environment.json').write_text(json.dumps({'owner': owner, 'pid': envworker, 'environment': {}}))
                write(owner='old'); self.assertFalse(media_ready(runtime, record))
                write(ready=False); self.assertFalse(media_ready(runtime, record))
                write(stamp=99); self.assertFalse(media_ready(runtime, record))
                write(envworker=21); self.assertFalse(media_ready(runtime, record))
                write(); self.assertTrue(media_ready(runtime, record))
                (media / 'control.sock').unlink(); self.assertFalse(media_ready(runtime, record))

    def test_prepared_fingerprint_changes_when_persisted_caps_change(self):
        from multicraft.pool_config import default_config, load_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / 'multicraft.json'; config.write_text(json.dumps(default_config()))
            before = launcher.launch_settings_fingerprint(load_config(config))
            (root / '.runtime').mkdir()
            (root / '.runtime/stream-policy.json').write_text(json.dumps({'revision': 1, 'limits': {
                'width': [320, 1920], 'height': [180, 1080], 'fps': [10, 30], 'bitrateKbps': [500, 4000]}}))
            self.assertNotEqual(before, launcher.launch_settings_fingerprint(load_config(config)))
