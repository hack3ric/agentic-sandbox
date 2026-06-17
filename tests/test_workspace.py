import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_vm.main import AgenticVM, AgenticVMError, Paths


class WorkspaceTests(unittest.TestCase):
    def make_paths(self, root: Path) -> Paths:
        repo_root = root / "repo"
        template_dir = repo_root / "mkosi"
        home = root / "home"
        data_dir = root / "data"
        state_dir = root / "state"
        image_dir = data_dir / "base-image"
        template_dir.mkdir(parents=True)
        home.mkdir()
        data_dir.mkdir()
        state_dir.mkdir()
        image_dir.mkdir()
        return Paths(
            repo_root=repo_root,
            template_dir=template_dir,
            home=home,
            data_dir=data_dir,
            state_dir=state_dir,
            image_dir=image_dir,
            build_marker=image_dir / ".image-built.json",
        )

    def test_workspace_stages_mirrorlist_and_preserves_script_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            (paths.template_dir / "mkosi.conf.in").write_text(
                "[Content]\nPackages=@PACKAGES@\n", encoding="utf-8"
            )
            postinst = paths.template_dir / "mkosi.postinst"
            postinst.write_text("#!/bin/sh\nprintf '%s\n' '@PACKAGES@'\n", encoding="utf-8")
            postinst.chmod(0o755)
            host_mirrorlist = root / "mirrorlist"
            host_mirrorlist.write_text(
                "Server = https://example.invalid/$repo/os/$arch\n",
                encoding="utf-8",
            )

            app = AgenticVM(paths, paths.repo_root)
            with patch("agentic_vm.main.HOST_PACMAN_MIRRORLIST", host_mirrorlist):
                app.ensure_mkosi_workspace()

            self.assertIn(
                "linux-headers",
                (paths.image_dir / "mkosi.conf").read_text(encoding="utf-8"),
            )
            for relative in (
                Path("mkosi.sandbox/etc/pacman.d/mirrorlist"),
                Path("mkosi.extra/etc/pacman.d/mirrorlist"),
            ):
                self.assertEqual(
                    (paths.image_dir / relative).read_text(encoding="utf-8"),
                    host_mirrorlist.read_text(encoding="utf-8"),
                )
            self.assertEqual(
                (paths.image_dir / "mkosi.postinst").stat().st_mode & 0o777,
                0o755,
            )
            self.assertIn(
                "@PACKAGES@",
                (paths.image_dir / "mkosi.postinst").read_text(encoding="utf-8"),
            )

    def test_workspace_requires_host_mirrorlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            (paths.template_dir / "mkosi.conf.in").write_text(
                "[Content]\nPackages=@PACKAGES@\n", encoding="utf-8"
            )
            app = AgenticVM(paths, paths.repo_root)
            missing = root / "missing-mirrorlist"

            with patch("agentic_vm.main.HOST_PACMAN_MIRRORLIST", missing):
                with self.assertRaises(AgenticVMError):
                    app.ensure_mkosi_workspace()


if __name__ == "__main__":
    unittest.main()
