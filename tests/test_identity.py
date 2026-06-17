import hashlib
import tempfile
import unittest
from pathlib import Path

from agentic_sandbox.main import AgenticVM, MAX_MACHINE_NAME_LENGTH, Paths


class IdentityTests(unittest.TestCase):
    def make_paths(self, root: Path) -> Paths:
        repo_root = root / "repo"
        template_dir = repo_root / "mkosi"
        home = root / "home"
        data_dir = root / "data"
        state_dir = root / "state"
        image_dir = data_dir / "base-image"
        podman_image_dir = data_dir / "podman-image"
        template_dir.mkdir(parents=True)
        (repo_root / "podman").mkdir(parents=True)
        home.mkdir()
        data_dir.mkdir()
        state_dir.mkdir()
        image_dir.mkdir()
        podman_image_dir.mkdir()
        return Paths(
            repo_root=repo_root,
            template_dir=template_dir,
            home=home,
            data_dir=data_dir,
            state_dir=state_dir,
            image_dir=image_dir,
            build_marker=image_dir / ".image-built.json",
            podman_template_dir=repo_root / "podman",
            podman_image_dir=podman_image_dir,
            podman_build_marker=podman_image_dir / ".image-built.json",
        )

    def test_identity_encodes_workspace_path_in_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "Project Name.with-symbols"
            cwd.mkdir()

            app = AgenticVM(paths, cwd, backend=object())
            identity = app.identity_for()
            digest = hashlib.sha256(str(cwd.resolve()).encode("utf-8")).hexdigest()[:12]
            encoded_path = app.encode_path_for_name(cwd.resolve())

            self.assertEqual(identity.vm_id, digest)
            self.assertIn(encoded_path[:20], identity.machine_name)
            self.assertTrue(identity.machine_name.endswith(f"-{digest}"))
            self.assertEqual(identity.unit_name, f"{identity.machine_name}.service")

    def test_identity_truncates_encoded_path_to_machine_name_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / ("very-" * 20 + "long")
            cwd.mkdir()

            app = AgenticVM(paths, cwd, backend=object())
            identity = app.identity_for()

            self.assertLessEqual(len(identity.machine_name), MAX_MACHINE_NAME_LENGTH)
            self.assertTrue(identity.machine_name.startswith("agentic-sandbox-"))
            self.assertTrue(identity.unit_name.endswith(".service"))


if __name__ == "__main__":
    unittest.main()
