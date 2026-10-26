import contextlib
import io
import shlex
import tempfile
import unittest
from pathlib import Path

from agentic_sandbox.main import AgenticSandbox
from agentic_sandbox.mkosi_backend import GUEST_WORK_MOUNT, MkosiBackend

from tests.helpers import make_paths


class SandboxMountTests(unittest.TestCase):
    def test_create_mounts_workspace_at_fixed_guest_path_and_adds_host_bind_mounts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            (paths.home / ".codex").mkdir()
            (paths.home / ".claude").mkdir()

            commands = []

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            def runner(command, **kwargs):
                commands.append(command)
                return Result()

            backend = MkosiBackend(paths, runner=runner)
            app = AgenticSandbox(paths, cwd, backend=backend)
            app.ensure_directories = lambda: None
            app.prune_stale_state = lambda identity: None
            app.write_state = lambda identity: None
            backend.is_running = lambda identity: False
            backend.ensure_mkosi_workspace = lambda force=False: None
            backend.ensure_ssh_credentials = lambda: None
            backend.ensure_image_built = lambda: None
            backend.wait_for_machine = lambda identity: commands.append(
                ["wait", identity.machine_name]
            )

            with contextlib.redirect_stdout(io.StringIO()):
                app.create(backend_args=["--", "--qemu-smp", "4"])

            self.assertEqual(commands[-1], ["wait", app.identity_for().machine_name])
            systemd_run = commands[0]
            self.assertIn("--runtime-tree", systemd_run)
            runtime_trees = [
                systemd_run[index + 1]
                for index, value in enumerate(systemd_run)
                if value == "--runtime-tree"
            ]
            self.assertIn(f"{cwd}:{GUEST_WORK_MOUNT}", runtime_trees)
            self.assertIn(f"{paths.home / '.codex'}:/root/.codex", runtime_trees)
            self.assertIn(f"{paths.home / '.claude'}:/root/.claude", runtime_trees)
            self.assertNotIn(
                f"{paths.home / '.config/opencode'}:/root/.config/opencode",
                runtime_trees,
            )
            self.assertEqual(
                systemd_run[systemd_run.index("--qemu-smp") + 1], "4"
            )
            self.assertLess(systemd_run.index("--qemu-smp"), systemd_run.index("vm"))

    def test_ssh_starts_interactive_session_after_binding_workspace_to_project_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            cwd = root / "project dir"
            cwd.mkdir()

            commands = []

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            def runner(command, **kwargs):
                commands.append(command)
                return Result()

            backend = MkosiBackend(paths, runner=runner)
            app = AgenticSandbox(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True

            app.exec_in_sandbox([])

            self.assertEqual(commands[0][-3], "--")
            self.assertEqual(commands[0][-2], "-t")
            remote_command = commands[0][-1]
            self.assertIn(f"mount --bind {GUEST_WORK_MOUNT} ", remote_command)
            self.assertIn(
                f"cd {shlex.quote(str(cwd))} && exec ${{SHELL:-/bin/bash}} -l",
                remote_command,
            )

    def test_ssh_runs_commands_from_project_directory_after_binding_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            cwd = root / "project dir"
            cwd.mkdir()

            commands = []

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            def runner(command, **kwargs):
                commands.append(command)
                return Result()

            backend = MkosiBackend(paths, runner=runner)
            app = AgenticSandbox(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True
            backend.should_allocate_ssh_tty = lambda: False

            app.exec_in_sandbox(["--", "pwd"])

            self.assertEqual(commands[0][-2], "--")
            remote_command = commands[0][-1]
            self.assertIn(f"mount --bind {GUEST_WORK_MOUNT} ", remote_command)
            self.assertTrue(
                remote_command.endswith(f"cd {shlex.quote(str(cwd))} && exec pwd")
            )

    def test_ssh_allocates_tty_for_terminal_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = make_paths(root)
            cwd = root / "project dir"
            cwd.mkdir()

            commands = []

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            def runner(command, **kwargs):
                commands.append(command)
                return Result()

            backend = MkosiBackend(paths, runner=runner)
            app = AgenticSandbox(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True
            backend.should_allocate_ssh_tty = lambda: True

            app.exec_in_sandbox(["--", "codex"])

            self.assertEqual(commands[0][-3], "--")
            self.assertEqual(commands[0][-2], "-t")
            remote_command = commands[0][-1]
            self.assertIn(f"mount --bind {GUEST_WORK_MOUNT} ", remote_command)
            self.assertTrue(
                remote_command.endswith(f"cd {shlex.quote(str(cwd))} && exec codex")
            )


if __name__ == "__main__":
    unittest.main()
