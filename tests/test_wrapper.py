import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class WrapperTests(unittest.TestCase):
    def test_wrapper_resolves_package_outside_repo_root(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("agentic-sandbox", "agentic-container", "agentic-vm"):
                with self.subTest(wrapper=name):
                    result = subprocess.run(
                        [str(repo_root / name), "--help"],
                        cwd=tmpdir,
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(result.returncode, 0, msg=result.stderr)
                    self.assertIn("usage: agentic-sandbox", result.stdout)

    def test_backend_wrappers_forward_arguments_and_exit_status(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        args = ["run", "--", "printf", "%s", "two words", "", "*.py", "$HOME"]

        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper_dir = Path(tmpdir) / "wrapper scripts"
            wrapper_dir.mkdir()
            sandbox = wrapper_dir / "agentic-sandbox"
            sandbox.write_text(
                '#!/usr/bin/env bash\nprintf "%s\\0" "$@"\nexit 7\n',
                encoding="utf-8",
            )
            sandbox.chmod(0o755)

            for name, backend in (
                ("agentic-container", "podman"),
                ("agentic-vm", "mkosi"),
            ):
                with self.subTest(wrapper=name):
                    wrapper = wrapper_dir / name
                    shutil.copy2(repo_root / name, wrapper)
                    result = subprocess.run(
                        [str(wrapper), *args],
                        cwd=tmpdir,
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(result.returncode, 7, msg=result.stderr)
                    self.assertEqual(
                        result.stdout.split("\0"), ["--backend", backend, *args, ""]
                    )


if __name__ == "__main__":
    unittest.main()
