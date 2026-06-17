import shlex
import tempfile
import unittest
from pathlib import Path

from agentic_vm.main import AgenticVM, GUEST_WORK_MOUNT, Paths


class VMMountTests(unittest.TestCase):
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

    def test_create_mounts_workspace_at_fixed_guest_path_and_adds_host_bind_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
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

            app = AgenticVM(paths, cwd, runner=runner)
            app.ensure_directories = lambda: None
            app.ensure_mkosi_workspace = lambda force=False: None
            app.ensure_ssh_credentials = lambda: None
            app.prune_stale_state = lambda identity: None
            app.is_unit_active = lambda unit_name: False
            app.ensure_image_built = lambda: None
            app.write_state = lambda identity: None

            app.create()

            systemd_run = commands[0]
            self.assertIn("--runtime-tree", systemd_run)
            runtime_trees = [
                systemd_run[index + 1]
                for index, value in enumerate(systemd_run)
                if value == "--runtime-tree"
            ]
            self.assertIn(f"{cwd}:{GUEST_WORK_MOUNT}", runtime_trees)
            self.assertIn(
                f"{paths.home / '.codex'}:/root/.codex", runtime_trees
            )
            self.assertIn(
                f"{paths.home / '.claude'}:/root/.claude", runtime_trees
            )
            self.assertNotIn(
                f"{paths.home / '.config/opencode'}:/root/.config/opencode",
                runtime_trees,
            )

    def test_ssh_starts_interactive_session_after_binding_workspace_to_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
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

            app = AgenticVM(paths, cwd, runner=runner)
            app.prune_stale_state = lambda identity: None
            app.is_unit_active = lambda unit_name: True

            app.ssh([])

            self.assertEqual(commands[0][-3], "--")
            self.assertEqual(commands[0][-2], "-t")
            remote_command = commands[0][-1]
            self.assertIn(f"mount --bind {GUEST_WORK_MOUNT} ", remote_command)
            self.assertIn(
                f"cd {shlex.quote(str(cwd))} && exec ${{SHELL:-/bin/bash}} -l",
                remote_command,
            )

    def test_ssh_runs_commands_from_project_directory_after_binding_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
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

            app = AgenticVM(paths, cwd, runner=runner)
            app.prune_stale_state = lambda identity: None
            app.is_unit_active = lambda unit_name: True
            app.should_allocate_ssh_tty = lambda: False

            app.ssh(["--", "pwd"])

            self.assertEqual(commands[0][-2], "--")
            remote_command = commands[0][-1]
            self.assertIn(f"mount --bind {GUEST_WORK_MOUNT} ", remote_command)
            self.assertTrue(
                remote_command.endswith(f"cd {shlex.quote(str(cwd))} && exec pwd")
            )

    def test_ssh_allocates_tty_for_terminal_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
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

            app = AgenticVM(paths, cwd, runner=runner)
            app.prune_stale_state = lambda identity: None
            app.is_unit_active = lambda unit_name: True
            app.should_allocate_ssh_tty = lambda: True

            app.ssh(["--", "codex"])

            self.assertEqual(commands[0][-3], "--")
            self.assertEqual(commands[0][-2], "-t")
            remote_command = commands[0][-1]
            self.assertIn(f"mount --bind {GUEST_WORK_MOUNT} ", remote_command)
            self.assertTrue(
                remote_command.endswith(f"cd {shlex.quote(str(cwd))} && exec codex")
            )


if __name__ == "__main__":
    unittest.main()
