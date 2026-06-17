import subprocess
import tempfile
import unittest
from pathlib import Path


class WrapperTests(unittest.TestCase):
    def test_wrapper_resolves_package_outside_repo_root(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        wrapper = repo_root / "agentic-vm"

        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [str(wrapper), "--help"],
                cwd=tmpdir,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("usage: agentic-vm", result.stdout)


if __name__ == "__main__":
    unittest.main()
