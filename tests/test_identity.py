import hashlib
import tempfile
import unittest
from pathlib import Path

from agentic_sandbox.main import MAX_MACHINE_NAME_LENGTH, AgenticSandbox

from tests.helpers import make_paths


class IdentityTests(unittest.TestCase):
    def test_identity_encodes_workspace_path_in_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            cwd = root / "Project Name.with-symbols"
            cwd.mkdir()

            app = AgenticSandbox(paths, cwd, backend=object())
            identity = app.identity_for()
            digest = hashlib.sha256(str(cwd.resolve()).encode("utf-8")).hexdigest()[:12]
            encoded_path = app.encode_path_for_name(cwd.resolve())

            self.assertEqual(identity.sandbox_id, digest)
            self.assertIn(encoded_path[:20], identity.machine_name)
            self.assertTrue(identity.machine_name.endswith(f"-{digest}"))
            self.assertEqual(identity.unit_name, f"{identity.machine_name}.service")

    def test_identity_truncates_encoded_path_to_machine_name_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            cwd = root / ("very-" * 20 + "long")
            cwd.mkdir()

            app = AgenticSandbox(paths, cwd, backend=object())
            identity = app.identity_for()

            self.assertLessEqual(len(identity.machine_name), MAX_MACHINE_NAME_LENGTH)
            self.assertTrue(identity.machine_name.startswith("agentic-sandbox-"))
            self.assertTrue(identity.unit_name.endswith(".service"))


if __name__ == "__main__":
    unittest.main()
