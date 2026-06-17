import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sandbox.main import AgenticVM, AgenticVMError, Paths
from agentic_sandbox.podman_backend import PodmanBackend


class PodmanBackendTests(unittest.TestCase):
    def make_paths(self, root: Path) -> Paths:
        repo_root = root / "repo"
        template_dir = repo_root / "mkosi"
        podman_template_dir = repo_root / "podman"
        home = root / "home"
        data_dir = root / "data"
        state_dir = root / "state"
        image_dir = data_dir / "base-image"
        podman_image_dir = data_dir / "podman-image"
        template_dir.mkdir(parents=True)
        podman_template_dir.mkdir(parents=True)
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
            podman_template_dir=podman_template_dir,
            podman_image_dir=podman_image_dir,
            podman_build_marker=podman_image_dir / ".image-built.json",
        )

    def make_backend(self, paths: Paths, commands: list[list[str]]) -> PodmanBackend:
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def runner(command, **kwargs):
            commands.append(command)
            if command[:3] == ["podman", "container", "exists"]:
                return Result(returncode=1)
            if command[:4] == ["podman", "container", "inspect", "--format"]:
                return Result(stdout="false\n")
            return Result()

        return PodmanBackend(paths, runner=runner, error_type=AgenticVMError)

    def test_create_builds_creates_and_starts_only_when_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            commands = []
            backend = self.make_backend(paths, commands)
            app = AgenticVM(paths, cwd, backend=backend)
            identity = app.identity_for()

            backend.ensure_workspace = lambda force=False: None
            backend.wait_for_container = lambda identity: commands.append(  # pyright: ignore[reportAttributeAccessIssue]
                ["wait", identity.machine_name]
            )

            backend.create(identity, cwd, wait=False)
            backend.create(identity, cwd, wait=True)

            self.assertEqual(commands[0][:2], ["podman", "build"])
            self.assertTrue(
                any(command[:2] == ["podman", "create"] for command in commands)
            )
            self.assertTrue(
                all(
                    "sudo" not in " ".join(command)
                    for command in commands
                    if isinstance(command, list)
                )
            )
            self.assertIn(["podman", "start", identity.machine_name], commands)
            self.assertIn(["wait", identity.machine_name], commands)

    def test_ssh_runs_in_project_directory_and_selects_interactive_shell(self) -> None:
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

            backend = PodmanBackend(paths, runner=runner, error_type=AgenticVMError)
            app = AgenticVM(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True

            app.ssh([])

            command = commands[0]
            self.assertEqual(command[:3], ["podman", "exec", "--workdir"])
            self.assertIn("-it", command)
            self.assertIn(app.identity_for().machine_name, command)
            self.assertTrue(command[-1].endswith("exec ${SHELL:-/bin/bash} -l"))
            self.assertNotIn("sudo", " ".join(command))

    def test_ssh_runs_command_without_tty_when_not_needed(self) -> None:
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

            backend = PodmanBackend(paths, runner=runner, error_type=AgenticVMError)
            app = AgenticVM(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True
            backend.should_allocate_tty = lambda: False

            app.ssh(["--", "pwd"])

            command = commands[0]
            self.assertNotIn("-it", command)
            self.assertTrue(command[-1].endswith(f"cd '{cwd}' && exec pwd"))

    def test_stop_uses_graceful_then_cleanup_and_force_uses_rm_f(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            commands = []

            class Result:
                def __init__(self, returncode=0):
                    self.returncode = returncode
                    self.stdout = ""
                    self.stderr = ""

            def runner(command, **kwargs):
                commands.append(command)
                if command[:3] == ["podman", "container", "exists"]:
                    return Result(0)
                return Result()

            backend = PodmanBackend(paths, runner=runner, error_type=AgenticVMError)
            identity = AgenticVM(paths, cwd, backend=backend).identity_for()

            backend.stop(
                identity, force=False, timeout_seconds=7.9, poll_interval_seconds=1.0
            )
            backend.stop(
                identity, force=True, timeout_seconds=7.9, poll_interval_seconds=1.0
            )

            self.assertIn(
                ["podman", "stop", "--time", "7", identity.machine_name], commands
            )
            self.assertIn(["podman", "rm", identity.machine_name], commands)
            self.assertIn(
                ["podman", "rm", "-f", "-t", "0", identity.machine_name], commands
            )

    def test_rebuild_refuses_when_selected_backend_has_active_managed_states(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            other = root / "other"
            other.mkdir()
            podman_backend = PodmanBackend(paths, runner=lambda command, **kwargs: None)
            app = AgenticVM(paths, cwd, backend=podman_backend)
            podman_identity = app.identity_for(other)
            podman_identity.state_file.write_text(
                json.dumps(
                    {
                        "cwd": str(other),
                        "vm_id": podman_identity.vm_id,
                        "unit_name": podman_identity.unit_name,
                        "machine_name": podman_identity.machine_name,
                        "image_dir": str(paths.podman_image_dir),
                        "backend": "podman",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (
                paths.state_dir / f"{app.identity_for(root / 'mkosi').vm_id}.json"
            ).write_text(
                json.dumps({"backend": "mkosi"}) + "\n",
                encoding="utf-8",
            )
            podman_backend.is_running = lambda identity: True

            with self.assertRaises(AgenticVMError):
                app.rebuild()

    def test_mount_args_include_workspace_and_existing_optional_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            (paths.home / ".codex").mkdir()
            (paths.home / ".config").mkdir()
            (paths.home / ".config" / "opencode").mkdir()

            backend = PodmanBackend(paths, runner=lambda command, **kwargs: None)
            mount_args = backend.mount_args(cwd)
            mounts = [
                mount_args[index + 1]
                for index, value in enumerate(mount_args)
                if value == "--volume"
            ]

            self.assertIn(f"{cwd}:{cwd}", mounts)
            self.assertIn(
                f"{paths.home / '.codex'}:/root/.codex",
                mounts,
            )
            self.assertIn(
                f"{paths.home / '.config/opencode'}:/root/.config/opencode",
                mounts,
            )

    def test_workspace_stages_containerfile_and_host_mirrorlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            containerfile = paths.podman_template_dir / "Containerfile.in"
            containerfile.write_text(
                "RUN pacman -S --noconfirm \\\n        @PACKAGES@\n", encoding="utf-8"
            )
            host_mirrorlist = root / "mirrorlist"
            host_mirrorlist.write_text(
                "Server = https://example.invalid/$repo/os/$arch\n", encoding="utf-8"
            )

            backend = PodmanBackend(paths, error_type=AgenticVMError)
            with patch(
                "agentic_sandbox.podman_backend.HOST_PACMAN_MIRRORLIST", host_mirrorlist
            ):
                backend.ensure_workspace()

            self.assertIn(
                "archlinux-keyring",
                (paths.podman_image_dir / "Containerfile").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (paths.podman_image_dir / "host-mirrorlist").read_text(
                    encoding="utf-8"
                ),
                host_mirrorlist.read_text(encoding="utf-8"),
            )
