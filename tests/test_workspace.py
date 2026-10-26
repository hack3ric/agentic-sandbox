import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sandbox.main import AgenticSandboxError
from agentic_sandbox.mkosi_backend import MkosiBackend

from tests.helpers import make_paths


class WorkspaceTests(unittest.TestCase):
    def test_workspace_stages_mirrorlist_and_preserves_script_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            (paths.template_dir / "mkosi.conf.in").write_text(
                "[Content]\nPackages=@PACKAGES@\n", encoding="utf-8"
            )
            postinst = paths.template_dir / "mkosi.postinst"
            postinst.write_text(
                "#!/bin/sh\nprintf '%s\n' '@PACKAGES@'\n", encoding="utf-8"
            )
            postinst.chmod(0o755)
            repart = paths.template_dir / "mkosi.extra/usr/lib/repart.d/10-root.conf"
            repart.parent.mkdir(parents=True, exist_ok=True)
            repart.write_text(
                "[Partition]\nType=root\nGrowFileSystem=yes\n",
                encoding="utf-8",
            )
            sysusers = (
                paths.template_dir
                / "mkosi.extra/usr/lib/sysusers.d/agentic-sandbox.conf"
            )
            sysusers.parent.mkdir(parents=True, exist_ok=True)
            sysusers.write_text(
                'u someone - "Agentic Sandbox User" /home/someone /bin/bash\n'
                "m someone wheel\n",
                encoding="utf-8",
            )
            tmpfiles = (
                paths.template_dir
                / "mkosi.extra/usr/lib/tmpfiles.d/agentic-sandbox.conf"
            )
            tmpfiles.parent.mkdir(parents=True, exist_ok=True)
            tmpfiles.write_text(
                "d /home/someone 0755 someone someone -\n",
                encoding="utf-8",
            )
            host_mirrorlist = root / "mirrorlist"
            host_mirrorlist.write_text(
                "Server = https://example.invalid/$repo/os/$arch\n",
                encoding="utf-8",
            )

            backend = MkosiBackend(paths, error_type=AgenticSandboxError)
            with patch(
                "agentic_sandbox.mkosi_backend.HOST_PACMAN_MIRRORLIST", host_mirrorlist
            ):
                backend.ensure_mkosi_workspace()

            self.assertIn(
                "archlinux-keyring",
                (paths.image_dir / "mkosi.conf").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "linux-headers",
                (paths.image_dir / "mkosi.conf").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "sudo",
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
            self.assertEqual(
                (
                    paths.image_dir / "mkosi.extra/usr/lib/repart.d/10-root.conf"
                ).read_text(encoding="utf-8"),
                "[Partition]\nType=root\nGrowFileSystem=yes\n",
            )
            self.assertEqual(
                (
                    paths.image_dir
                    / "mkosi.extra/usr/lib/sysusers.d/agentic-sandbox.conf"
                ).read_text(encoding="utf-8"),
                'u someone - "Agentic Sandbox User" /home/someone /bin/bash\n'
                "m someone wheel\n",
            )
            self.assertEqual(
                (
                    paths.image_dir
                    / "mkosi.extra/usr/lib/tmpfiles.d/agentic-sandbox.conf"
                ).read_text(encoding="utf-8"),
                "d /home/someone 0755 someone someone -\n",
            )

    def test_postinst_initializes_keyring_and_writes_sudoers(self) -> None:
        postinst = (
            Path(__file__).resolve().parents[1]
            / "agentic_sandbox" / "mkosi" / "mkosi.postinst"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "image root"
            (root / "etc").mkdir(parents=True)
            (root / "etc/pacman.conf").write_text("[options]\n", encoding="utf-8")
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            calls_file = Path(tmpdir) / "keyring-calls"
            pacman_key = bin_dir / "pacman-key"
            pacman_key.write_text(
                '#!/bin/sh\nprintf "%s\\0" pacman-key "$@" >> "$TEST_KEYRING_CALLS"\n',
                encoding="utf-8",
            )
            pacman_key.chmod(0o755)
            fakeroot = bin_dir / "fakeroot"
            fakeroot.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
            fakeroot.chmod(0o755)
            result = subprocess.run(
                ["sh", str(postinst)],
                env={
                    **os.environ,
                    "BUILDROOT": str(root),
                    "PATH": f"{bin_dir}:{os.environ.get('PATH', os.defpath)}",
                    "TEST_KEYRING_CALLS": str(calls_file),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            keyring = root / "etc/pacman.d/gnupg"
            self.assertTrue(keyring.is_dir())
            self.assertEqual(
                calls_file.read_text(encoding="utf-8").split("\0")[:-1],
                [
                    "pacman-key", "--gpgdir", str(keyring), "--init",
                    "pacman-key", "--gpgdir", str(keyring), "--populate", "archlinux",
                ],
            )
            sudoers = root / "etc/sudoers.d/someone"
            self.assertEqual(
                sudoers.read_text(encoding="utf-8").strip(),
                "someone ALL=(ALL) NOPASSWD: ALL",
            )
            self.assertEqual(sudoers.stat().st_mode & 0o777, 0o440)

    def test_workspace_materializes_symlink_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            (paths.template_dir / "mask.service.symlink").write_text(
                "/dev/null\n",
                encoding="utf-8",
            )
            host_mirrorlist = root / "mirrorlist"
            host_mirrorlist.write_text(
                "Server = https://example.invalid/$repo/os/$arch\n",
                encoding="utf-8",
            )

            backend = MkosiBackend(paths, error_type=AgenticSandboxError)
            with patch(
                "agentic_sandbox.mkosi_backend.HOST_PACMAN_MIRRORLIST", host_mirrorlist
            ):
                backend.ensure_mkosi_workspace()

            target = paths.image_dir / "mask.service"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.readlink(), Path("/dev/null"))

    def test_workspace_requires_host_mirrorlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            (paths.template_dir / "mkosi.conf.in").write_text(
                "[Content]\nPackages=@PACKAGES@\n", encoding="utf-8"
            )
            backend = MkosiBackend(paths, error_type=AgenticSandboxError)
            missing = root / "missing-mirrorlist"

            with (
                patch("agentic_sandbox.mkosi_backend.HOST_PACMAN_MIRRORLIST", missing),
                self.assertRaises(AgenticSandboxError),
            ):
                backend.ensure_mkosi_workspace()


if __name__ == "__main__":
    unittest.main()
