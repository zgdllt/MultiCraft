from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


SPEC = spec_from_file_location("release_source", Path(__file__).resolve().parents[1] / "scripts/release_source.py")
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "VERSION").write_text("0.1.0-alpha.1\n")
        (self.root / "README.md").write_text("MultiCraft public source\n")
        (self.root / "web").mkdir()
        (self.root / "web" / "app.js").write_text("export const example = 1;\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_runtime_and_unknown_roots_cannot_enter_archive(self):
        (self.root / ".runtime").mkdir()
        (self.root / ".runtime" / "server.key").write_text("private")
        (self.root / "accounts.json").write_text("local account registry")
        paths = MODULE.source_files(self.root)
        self.assertEqual([str(path.relative_to(self.root)) for path in paths], ["README.md", "VERSION", "web/app.js"])

    def test_private_artifact_inside_source_root_is_rejected(self):
        (self.root / "web" / "client.jar").write_bytes(b"not publishable")
        with self.assertRaisesRegex(MODULE.AuditError, "artifact"):
            MODULE.source_files(self.root)

    def test_private_material_is_rejected_without_printing_material(self):
        secret = "-----BEGIN " + "PRIVATE KEY-----"
        (self.root / "web" / "bad.txt").write_text(secret)
        with self.assertRaisesRegex(MODULE.AuditError, r"private key material: web/bad.txt:1"):
            MODULE.source_files(self.root)

    def test_symlink_cannot_escape_allowlist(self):
        (self.root / "web" / "outside.txt").symlink_to(self.root / "README.md")
        with self.assertRaisesRegex(MODULE.AuditError, "symlink"):
            MODULE.source_files(self.root)

    def test_browser_pkcs8_key_json_is_rejected_recursively(self):
        (self.root / "config").mkdir()
        key_file = self.root / "config" / "browser-key.json"
        key_file.write_text(json.dumps({"keys": [{"publicKey": "U1BLSS1URVNULVBVQkxJQw==",
                                                 "privateKey": "UEtDUzgtVEVTVC1QUklWQVRF"}]}))
        with self.assertRaisesRegex(MODULE.AuditError, r"private key field in JSON: config/browser-key.json"):
            MODULE.source_files(self.root)

    def test_javascript_private_key_property_is_valid_source(self):
        (self.root / "web" / "app.js").write_text("export const sign = keyPair => keyPair.privateKey;\n")
        self.assertTrue(MODULE.source_files(self.root))

    def test_archive_has_stable_hashes_and_no_host_metadata(self):
        paths = MODULE.source_files(self.root)
        receipt = MODULE.manifest(self.root, paths)
        first, second = self.root / "first.tar.gz", self.root / "second.tar.gz"
        MODULE.archive(self.root, paths, first, receipt)
        MODULE.archive(self.root, paths, second, receipt)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(first.read_bytes()), mode="r:gz") as bundle:
            self.assertIn("MultiCraft/SOURCE-MANIFEST.json", bundle.getnames())
            self.assertTrue(all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in bundle.getmembers()))


if __name__ == "__main__":
    unittest.main()
