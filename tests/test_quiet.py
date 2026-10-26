import io
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agentic_sandbox.main import Paths, main
from agentic_sandbox.mkosi_backend import MkosiBackend
from agentic_sandbox.podman_backend import PodmanBackend


class TerminalOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


class QuietTests(unittest.TestCase):
    def test_quiet_disables_spinners_and_preserves_command_output(self) -> None:
        for backend_type in (MkosiBackend, PodmanBackend):
            for command in ("create", "run"):
                for flags in ([], ["-q"], ["--quiet"]):
                    with (
                        self.subTest(
                            backend=backend_type.name, command=command, flags=flags
                        ),
                        tempfile.TemporaryDirectory() as tmpdir,
                    ):
                        paths = replace(Paths.detect(), state_dir=Path(tmpdir))
                        stdout = io.StringIO()
                        stderr = TerminalOutput()
                        calls = []

                        def runner(args, **kwargs):
                            calls.append(args)
                            if kwargs.get("capture_output"):
                                return subprocess.CompletedProcess(args, 0, "", "")
                            print("command output")
                            print("command error", file=sys.stderr)
                            return subprocess.CompletedProcess(args, 7)

                        with (
                            patch("agentic_sandbox.main.Paths.detect", return_value=paths),
                            patch.object(backend_type, "is_running", return_value=True),
                            patch("subprocess.run", side_effect=runner),
                            patch("sys.stdout", stdout),
                            patch("sys.stderr", stderr),
                        ):
                            result = main(
                                ["-b", backend_type.name, command, *flags]
                                + (["--", "example"] if command == "run" else [])
                            )

                        self.assertTrue(any(call[-1] == "true" for call in calls))
                        self.assertEqual(result, 7 if command == "run" else 0)
                        if flags:
                            self.assertEqual(
                                stdout.getvalue(),
                                "command output\n" if command == "run" else "",
                            )
                            self.assertEqual(
                                stderr.getvalue(),
                                "command error\n" if command == "run" else "",
                            )
                        else:
                            self.assertIn("Waiting for", stderr.getvalue())
                            if command == "run":
                                self.assertIn("command output\n", stdout.getvalue())
                                self.assertIn("command error\n", stderr.getvalue())
