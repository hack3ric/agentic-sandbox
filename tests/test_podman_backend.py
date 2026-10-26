import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from agentic_sandbox.main import AgenticSandbox, AgenticSandboxError, Paths
from agentic_sandbox.podman_backend import (
    ARCH_LINUX_IMAGE,
    BASE_IMAGE_VERSION,
    GRAPHICS_LABEL,
    GUI_IMAGE_VERSION,
    GUI_LABEL,
    GUI_RUNTIME_DIR,
    GUI_WAYLAND_DISPLAY,
    KVM_DEVICE,
    KVM_LABEL,
    PodmanBackend,
)


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

    def make_render_device(
        self, root: Path, name: str, vendor: str | None = None, driver: str | None = None
    ) -> Path:
        device = root / "dri" / name
        device.parent.mkdir(exist_ok=True)
        device.touch()
        sysfs_device = root / "sysfs" / name / "device"
        sysfs_device.mkdir(parents=True)
        if vendor is not None:
            (sysfs_device / "vendor").write_text(vendor + "\n", encoding="utf-8")
        if driver is not None:
            driver_dir = root / "drivers" / driver
            driver_dir.mkdir(parents=True, exist_ok=True)
            (sysfs_device / "driver").symlink_to(driver_dir, target_is_directory=True)
        return device

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

        return PodmanBackend(paths, runner=runner, error_type=AgenticSandboxError)

    def test_create_always_starts_and_waits_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            commands = []
            backend = self.make_backend(paths, commands)
            app = AgenticSandbox(paths, cwd, backend=backend)
            identity = app.identity_for()

            backend.ensure_workspace = lambda force=False: None
            backend.wait_for_container = lambda identity: commands.append(  # pyright: ignore[reportAttributeAccessIssue]
                ["wait", identity.machine_name]
            )

            backend.create(identity, cwd, wait=False)
            self.assertEqual(commands[-1], ["podman", "start", identity.machine_name])
            self.assertNotIn(["wait", identity.machine_name], commands)

            backend.create(identity, cwd)
            self.assertEqual(commands[-1], ["wait", identity.machine_name])

            build_index = next(
                i for i, cmd in enumerate(commands) if cmd[:2] == ["podman", "build"]
            )
            create_index = next(
                i for i, cmd in enumerate(commands) if cmd[:2] == ["podman", "create"]
            )
            self.assertLess(build_index, create_index)
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
            self.assertEqual(
                [cmd for cmd in commands if cmd[:2] == ["podman", "build"]],
                [backend.build_cmd()],
            )
            self.assertFalse(backend.gui_build_marker.exists())

    def test_create_cmd_places_backend_args_before_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            backend = PodmanBackend(paths)
            identity = AgenticSandbox(paths, cwd, backend=backend).identity_for()

            command = backend.create_cmd(
                identity, cwd, ["--", "--cpus", "2", "--network", "host"]
            )

            image_index = command.index(backend.image_tag)
            self.assertEqual(
                command[image_index - 4 : image_index],
                ["--cpus", "2", "--network", "host"],
            )

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

            backend = PodmanBackend(
                paths, runner=runner, error_type=AgenticSandboxError
            )
            app = AgenticSandbox(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True

            app.exec_in_sandbox([])

            command = next(cmd for cmd in commands if cmd[:2] == ["podman", "exec"])
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

            backend = PodmanBackend(
                paths, runner=runner, error_type=AgenticSandboxError
            )
            app = AgenticSandbox(paths, cwd, backend=backend)
            app.prune_stale_state = lambda identity: None
            backend.is_running = lambda identity: True
            backend.should_allocate_tty = lambda: False

            app.exec_in_sandbox(["--", "pwd"])

            command = next(cmd for cmd in commands if cmd[:2] == ["podman", "exec"])
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

            backend = PodmanBackend(
                paths, runner=runner, error_type=AgenticSandboxError
            )
            identity = AgenticSandbox(paths, cwd, backend=backend).identity_for()

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

    def test_rebuild_allows_active_managed_states(
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
            app = AgenticSandbox(paths, cwd, backend=podman_backend)
            podman_identity = app.identity_for(other)
            podman_identity.state_file.write_text(
                json.dumps(
                    {
                        "cwd": str(other),
                        "sandbox_id": podman_identity.sandbox_id,
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
                paths.state_dir / f"{app.identity_for(root / 'mkosi').sandbox_id}.json"
            ).write_text(
                json.dumps({"backend": "mkosi"}) + "\n",
                encoding="utf-8",
            )
            rebuild_calls = []
            podman_backend.is_running = lambda identity: True
            podman_backend.rebuild = lambda: rebuild_calls.append(True)

            with patch("builtins.print") as print_mock:
                app.rebuild()

            self.assertEqual(rebuild_calls, [True])
            print_mock.assert_called_once_with("rebuilt shared image")

    def test_rebuild_updates_base_and_defers_gui_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self.make_paths(Path(tmpdir))
            commands = []
            backend = self.make_backend(paths, commands)
            backend.ensure_workspace = lambda force=False: None
            backend.write_build_marker()
            backend.write_build_marker(gui=True)

            backend.rebuild()

            self.assertEqual(
                commands,
                [["podman", "pull", ARCH_LINUX_IMAGE], backend.build_cmd()],
            )
            self.assertTrue(backend.image_is_built())
            self.assertFalse(backend.gui_build_marker.exists())
            backend.ensure_image_built(graphics=True)
            self.assertEqual(commands[2:], [backend.build_cmd(gui=True)])

    def test_failed_image_build_invalidates_markers_and_prevents_creation(self) -> None:
        for gui in (False, True):
            with self.subTest(gui=gui), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)
                commands = []

                def runner(command, **kwargs):
                    commands.append(command)
                    if command[:3] == ["podman", "container", "exists"]:
                        return subprocess.CompletedProcess(command, 1, "", "")
                    if command[:2] == ["podman", "build"]:
                        self.assertFalse(backend.gui_build_marker.exists())
                        self.assertEqual(backend.build_marker.exists(), gui)
                        raise subprocess.CalledProcessError(1, command)
                    return subprocess.CompletedProcess(command, 0, "", "")

                backend = PodmanBackend(paths, runner=runner)
                backend.write_build_marker()
                backend.write_build_marker(gui=True)
                stale_marker = backend.gui_build_marker if gui else backend.build_marker
                stale_marker.write_text(
                    json.dumps({"image_version": 0}), encoding="utf-8"
                )
                identity = AgenticSandbox(paths, root, backend=backend).identity_for()
                with (
                    patch.object(backend, "graphics_args", return_value=[]),
                    patch.object(backend, "ensure_workspace"),
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    backend.create(identity, root, graphics=True)
                self.assertEqual(
                    commands,
                    [
                        ["podman", "container", "exists", identity.machine_name],
                        backend.build_cmd(gui=gui),
                    ],
                )
                self.assertFalse(backend.gui_build_marker.exists())
                self.assertEqual(backend.image_is_built(), gui)

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

    def test_workspace_separates_base_and_gui_packages_and_stages_mirrorlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            host_mirrorlist = root / "mirrorlist"
            host_mirrorlist.write_text(
                "Server = https://example.invalid/$repo/os/$arch\n", encoding="utf-8"
            )

            backend = PodmanBackend(paths, error_type=AgenticSandboxError)
            backend.template_dir = (
                Path(__file__).resolve().parents[1] / "agentic_sandbox" / "podman"
            )
            with patch(
                "agentic_sandbox.podman_backend.HOST_PACMAN_MIRRORLIST", host_mirrorlist
            ):
                backend.ensure_workspace()

            base = (paths.podman_image_dir / "Containerfile").read_text(encoding="utf-8")
            gui = (paths.podman_image_dir / "Containerfile.gui").read_text(
                encoding="utf-8"
            )
            self.assertIn("archlinux-keyring", base)
            self.assertIn("sudo", base)
            self.assertTrue(gui.startswith(f"FROM {backend.image_tag}\n"))
            for package in (
                "mesa",
                "vulkan-icd-loader",
                "vulkan-intel",
                "vulkan-nouveau",
                "vulkan-radeon",
                "xwayland-satellite",
                "xorg-xdpyinfo",
            ):
                self.assertNotIn(package, base.split())
                self.assertIn(package, gui.split())
            self.assertNotIn("@", base)
            self.assertNotIn("@", gui)
            gui_build = backend.build_cmd(gui=True)
            self.assertIn("--pull=never", gui_build)
            self.assertEqual(
                gui_build[gui_build.index("--tag") + 1], backend.gui_image_tag
            )
            self.assertEqual(
                gui_build[gui_build.index("--file") + 1],
                str(paths.podman_image_dir / "Containerfile.gui"),
            )
            self.assertEqual(
                (paths.podman_image_dir / "host-mirrorlist").read_text(
                    encoding="utf-8"
                ),
                host_mirrorlist.read_text(encoding="utf-8"),
            )

    def test_repo_containerfile_creates_someone_sudoer(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        containerfile = (
            repo_root / "agentic_sandbox" / "podman" / "Containerfile.in"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "useradd --create-home --shell /bin/bash someone",
            containerfile,
        )
        self.assertIn(
            "'someone ALL=(ALL) NOPASSWD: ALL'",
            containerfile,
        )

    def test_gui_create_mounts_only_wayland_and_runs_private_xwayland(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            runtime_dir = root / "runtime dir"
            runtime_dir.mkdir()
            display = runtime_dir / "wayland-7"
            backend = PodmanBackend(paths)
            identity = AgenticSandbox(paths, root, backend=backend).identity_for()
            with (
                socket.socket(socket.AF_UNIX) as listener,
                patch.dict(
                    os.environ,
                    {
                        "XDG_RUNTIME_DIR": str(runtime_dir),
                        "WAYLAND_DISPLAY": display.name,
                    },
                    clear=True,
                ),
            ):
                listener.bind(str(display))
                command = backend.create_cmd(
                    identity, root, ["--", "--cpus", "2"], gui=True
                )

            mounts = [
                command[i + 1] for i, arg in enumerate(command) if arg == "--volume"
            ]
            self.assertEqual(
                mounts,
                [
                    f"{root}:{root}",
                    f"{display}:{GUI_RUNTIME_DIR / GUI_WAYLAND_DISPLAY}:ro",
                ],
            )
            env = [command[i + 1] for i, arg in enumerate(command) if arg == "--env"]
            self.assertIn("DISPLAY=:0", env)
            self.assertIn(f"WAYLAND_DISPLAY={GUI_WAYLAND_DISPLAY}", env)
            self.assertIn(f"XDG_RUNTIME_DIR={GUI_RUNTIME_DIR}", env)
            self.assertIn(f"{GUI_RUNTIME_DIR}:rw,mode=0700", command)
            self.assertIn("--init", command)
            self.assertIn(f"{GUI_LABEL}=wayland", command)
            image_index = command.index(backend.gui_image_tag)
            self.assertEqual(command[image_index - 2 : image_index], ["--cpus", "2"])
            self.assertEqual(
                command[image_index + 1 :],
                ["xwayland-satellite", ":0", "-nolisten", "tcp", "-shm"],
            )

    def test_gui_accepts_absolute_socket_without_runtime_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = PodmanBackend(self.make_paths(root))
            display = root / "wayland-absolute"
            with (
                socket.socket(socket.AF_UNIX) as listener,
                patch.dict(os.environ, {"WAYLAND_DISPLAY": str(display)}, clear=True),
            ):
                listener.bind(str(display))
                self.assertIn(
                    f"{display}:{GUI_RUNTIME_DIR / GUI_WAYLAND_DISPLAY}:ro",
                    backend.gui_args(),
                )

    def test_gui_uses_default_wayland_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = PodmanBackend(self.make_paths(root))
            display = root / "wayland-0"
            with (
                socket.socket(socket.AF_UNIX) as listener,
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root)}, clear=True),
            ):
                listener.bind(str(display))
                self.assertIn(
                    f"{display}:{GUI_RUNTIME_DIR / GUI_WAYLAND_DISPLAY}:ro",
                    backend.gui_args(),
                )

    def test_gui_rejects_invalid_display_before_build_or_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            regular_file = root / "regular-file"
            regular_file.touch()
            for env in (
                {},
                {"XDG_RUNTIME_DIR": "relative"},
                {"WAYLAND_DISPLAY": str(root / "missing")},
                {"WAYLAND_DISPLAY": str(regular_file)},
                {"WAYLAND_DISPLAY": str(root)},
            ):
                with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                    commands = []
                    backend = self.make_backend(paths, commands)
                    identity = AgenticSandbox(
                        paths, root, backend=backend
                    ).identity_for()
                    with self.assertRaises(AgenticSandboxError):
                        backend.create(identity, root, gui=True)
                    self.assertFalse(
                        any(
                            cmd[:2] in (["podman", "build"], ["podman", "create"])
                            for cmd in commands
                        )
                    )

    def test_gui_request_rejects_existing_non_gui_container(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)
                commands = []
                backend = self.make_backend(paths, commands)
                backend.is_known = lambda identity: True
                backend.is_running = lambda identity, running=running: running
                app = AgenticSandbox(paths, root, backend=backend)
                with self.assertRaisesRegex(
                    AgenticSandboxError, "was created without --gui"
                ):
                    app.create(gui=True)
                self.assertFalse(
                    any(
                        cmd[:2]
                        in (["podman", "build"], ["podman", "create"], ["podman", "rm"])
                        for cmd in commands
                    )
                )
                self.assertFalse(app.identity_for().state_file.exists())

    def test_gui_request_reuses_existing_gui_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            commands = []
            backend = self.make_backend(paths, commands)
            backend.is_known = lambda identity: True
            backend.is_running = lambda identity: True
            backend.gui_enabled = lambda identity: True
            app = AgenticSandbox(paths, root, backend=backend)
            with patch.object(backend, "wait_for_container") as wait:
                app.create(gui=True, wait=True, quiet=True)
            wait.assert_called_once_with(app.identity_for())
            self.assertEqual(commands, [])
            self.assertTrue(app.identity_for().state_file.exists())

    def test_legacy_image_migrates_to_base_then_builds_gui_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self.make_paths(Path(tmpdir))
            commands = []
            backend = self.make_backend(paths, commands)
            paths.podman_build_marker.write_text(
                json.dumps({"gui_version": 1, "graphics_version": 1}), encoding="utf-8"
            )
            backend.ensure_image_built()
            self.assertEqual(commands, [backend.build_cmd()])
            self.assertFalse(backend.gui_build_marker.exists())
            backend.ensure_image_built(gui=True)
            backend.ensure_image_built(graphics=True)
            backend.ensure_image_built()
            self.assertEqual(commands, [backend.build_cmd(), backend.build_cmd(gui=True)])
            marker = json.loads(paths.podman_build_marker.read_text(encoding="utf-8"))
            self.assertEqual(marker["image_version"], BASE_IMAGE_VERSION)
            gui_marker = json.loads(backend.gui_build_marker.read_text(encoding="utf-8"))
            self.assertEqual(gui_marker["image_version"], GUI_IMAGE_VERSION)

    def test_gui_wait_retries_until_xwayland_accepts_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            commands = []
            probes = 0
            sleeps = []

            def runner(command, **kwargs):
                nonlocal probes
                commands.append(command)
                if command[:2] == ["podman", "exec"]:
                    probes += 1
                    return subprocess.CompletedProcess(
                        command, 1 if probes == 1 else 0, "", ""
                    )
                output = "wayland" if GUI_LABEL in command[-2] else "true"
                return subprocess.CompletedProcess(command, 0, output, "")

            backend = PodmanBackend(paths, runner=runner, sleeper=sleeps.append)
            identity = AgenticSandbox(paths, root, backend=backend).identity_for()
            backend.wait_for_container(identity)
            self.assertEqual(probes, 2)
            self.assertEqual(sleeps, [1.0])
            self.assertEqual(
                commands[-1],
                ["podman", "exec", identity.machine_name, "timeout", "2", "xdpyinfo"],
            )

    def test_gui_wait_reports_timeout_and_helper_exit(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)

                def runner(command, **kwargs):
                    return subprocess.CompletedProcess(
                        command, 1, "", "display not ready"
                    )

                backend = PodmanBackend(
                    paths, runner=runner, error_type=AgenticSandboxError
                )
                backend.gui_enabled = lambda identity: True
                backend.is_running = lambda identity, running=running: running
                identity = AgenticSandbox(paths, root, backend=backend).identity_for()
                message = "timed out.*GUI display" if running else "not active"
                with self.assertRaisesRegex(AgenticSandboxError, message):
                    backend.wait_for_container(identity, timeout_seconds=0)

    def test_exec_waits_for_gui_from_container_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            commands = []
            backend = self.make_backend(paths, commands)
            backend.is_running = lambda identity: True
            backend.gui_enabled = lambda identity: True
            backend.should_allocate_tty = lambda: False
            backend.wait_for_container = lambda identity: commands.append(["wait"])
            identity = AgenticSandbox(paths, root, backend=backend).identity_for()
            backend.ssh(identity, root, ["--", "example-app"])
            self.assertEqual(commands[0], ["wait"])
            self.assertEqual(commands[1][:2], ["podman", "exec"])
            self.assertTrue(commands[1][-1].endswith("exec example-app"))

    def test_graphics_accepts_nouveau_and_excludes_nvidia_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = PodmanBackend(self.make_paths(root))
            amd = self.make_render_device(root, "renderD129", "0x1002", "amdgpu")
            intel = self.make_render_device(root, "renderD128", "0x8086", "i915")
            self.make_render_device(root, "renderD130", "0x10de")
            self.make_render_device(root, "renderD131", driver="nvidia")
            nouveau = self.make_render_device(root, "renderD132", "0x10de", "nouveau")
            virtio = self.make_render_device(root, "renderD133", driver="virtio_gpu")
            (root / "dri" / "card0").touch()
            with (
                patch("agentic_sandbox.podman_backend.DRI_DEVICE_DIR", root / "dri"),
                patch("agentic_sandbox.podman_backend.DRM_SYSFS_DIR", root / "sysfs"),
                patch.object(Path, "is_char_device", return_value=True),
                patch(
                    "agentic_sandbox.podman_backend.os.access", return_value=True
                ) as access,
            ):
                self.assertEqual(backend.graphics_devices(), [intel, amd, nouveau, virtio])
            self.assertEqual(
                access.call_args_list,
                [
                    call(device, os.R_OK | os.W_OK)
                    for device in (intel, amd, nouveau, virtio)
                ],
            )

    def test_graphics_rejects_unavailable_devices_before_building(self) -> None:
        for scenario, message in (
            ("missing", "requires a Mesa-compatible GPU render device"),
            ("nvidia", "requires a Mesa-compatible GPU render device"),
            ("unknown", "cannot identify GPU render device"),
            ("malformed", "cannot identify GPU render device"),
            ("regular_file", "requires a host character device"),
            ("inaccessible", "requires read/write access"),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)
                if scenario != "missing":
                    vendor = {
                        "nvidia": "0x10de",
                        "unknown": None,
                        "malformed": "invalid",
                    }.get(scenario, "0x8086")
                    self.make_render_device(root, "renderD128", vendor)
                commands = []
                backend = self.make_backend(paths, commands)
                identity = AgenticSandbox(paths, root, backend=backend).identity_for()
                with (
                    patch("agentic_sandbox.podman_backend.DRI_DEVICE_DIR", root / "dri"),
                    patch("agentic_sandbox.podman_backend.DRM_SYSFS_DIR", root / "sysfs"),
                    patch.object(
                        Path, "is_char_device", return_value=scenario != "regular_file"
                    ),
                    patch(
                        "agentic_sandbox.podman_backend.os.access",
                        return_value=scenario != "inaccessible",
                    ),
                    self.assertRaisesRegex(AgenticSandboxError, message),
                ):
                    backend.create(identity, root, graphics=True)
                self.assertEqual(
                    commands,
                    [["podman", "container", "exists", identity.machine_name]],
                )
                self.assertFalse(paths.podman_build_marker.exists())

    def test_graphics_create_exposes_devices_without_a_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            commands = []
            backend = self.make_backend(paths, commands)
            app = AgenticSandbox(paths, root, backend=backend)
            devices = [Path("/dev/dri/renderD128"), Path("/dev/dri/renderD129")]
            with (
                patch.object(backend, "graphics_devices", return_value=devices),
                patch.object(backend, "ensure_workspace"),
                patch.object(backend, "wait_for_container"),
                patch.dict(os.environ, {}, clear=True),
            ):
                app.create(graphics=True, quiet=True)
            command = next(cmd for cmd in commands if cmd[:2] == ["podman", "create"])
            image_index = command.index(backend.gui_image_tag)
            self.assertEqual(
                [cmd for cmd in commands if cmd[:2] == ["podman", "build"]],
                [backend.build_cmd(), backend.build_cmd(gui=True)],
            )
            self.assertLess(
                commands.index(backend.build_cmd(gui=True)), commands.index(command)
            )
            self.assertEqual(
                [command[i + 1] for i, arg in enumerate(command) if arg == "--device"],
                [f"{device}:{device}:rw" for device in devices],
            )
            self.assertIn(f"{GRAPHICS_LABEL}=enabled", command[:image_index])
            self.assertEqual(command[command.index("--group-add") + 1], "keep-groups")
            self.assertLess(command.index("--device"), image_index)
            self.assertEqual(command[image_index + 1], "/bin/sh")
            self.assertNotIn("--privileged", command)

    def test_graphics_gui_accelerates_xwayland_and_shares_kvm_group_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = PodmanBackend(self.make_paths(root))
            identity = AgenticSandbox(backend.paths, root, backend=backend).identity_for()
            with (
                patch.object(backend, "gui_args", return_value=[]),
                patch.object(
                    backend, "graphics_devices", return_value=[Path("/dev/dri/renderD128")]
                ),
                patch.object(Path, "is_char_device", return_value=True),
                patch("agentic_sandbox.podman_backend.os.access", return_value=True),
            ):
                command = backend.create_cmd(
                    identity, root, gui=True, graphics=True, kvm=True
                )
            self.assertEqual(
                command[command.index(backend.gui_image_tag) + 1 :],
                ["xwayland-satellite", ":0", "-nolisten", "tcp"],
            )
            self.assertIn("/dev/kvm:/dev/kvm:rw", command)
            self.assertEqual(command.count("--group-add"), 1)
            self.assertEqual(command.count("keep-groups"), 1)

    def test_graphics_request_rejects_existing_container_without_graphics(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)
                commands = []
                backend = self.make_backend(paths, commands)
                backend.is_known = lambda identity: True
                backend.is_running = lambda identity, running=running: running
                app = AgenticSandbox(paths, root, backend=backend)
                with self.assertRaisesRegex(
                    AgenticSandboxError, "was created without --graphics"
                ):
                    app.create(graphics=True, quiet=True)
                self.assertTrue(commands)
                self.assertTrue(
                    all(cmd[:3] == ["podman", "container", "inspect"] for cmd in commands)
                )
                self.assertFalse(app.identity_for().state_file.exists())

    def test_graphics_container_is_reused_by_create_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            commands = []
            running = False

            def runner(command, **kwargs):
                nonlocal running
                commands.append(command)
                output = ""
                if command[:2] == ["podman", "start"]:
                    running = True
                elif command[:3] == ["podman", "container", "inspect"]:
                    if command[-2] == "{{.State.Running}}":
                        output = str(running).lower()
                    elif GRAPHICS_LABEL in command[-2]:
                        output = "enabled"
                return subprocess.CompletedProcess(command, 0, output, "")

            backend = PodmanBackend(paths, runner=runner, spinner_enabled=False)
            backend.should_allocate_tty = lambda: False
            app = AgenticSandbox(paths, root, backend=backend)
            with patch.object(backend, "ensure_workspace") as workspace:
                app.create(graphics=True, quiet=True)
                app.create(graphics=True, quiet=True)
                self.assertEqual(app.run_sandbox(["--", "true"], quiet=True), 0)
            workspace.assert_not_called()
            self.assertFalse(backend.build_marker.exists())
            self.assertFalse(backend.gui_build_marker.exists())
            self.assertEqual(
                commands.count(["podman", "start", app.identity_for().machine_name]), 1
            )
            self.assertFalse(
                any(
                    cmd[:2] in (["podman", "create"], ["podman", "build"])
                    for cmd in commands
                )
            )
            self.assertEqual(commands[-1][:2], ["podman", "exec"])

    def test_graphics_rebuilds_only_outdated_gui_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self.make_paths(Path(tmpdir))
            commands = []
            backend = self.make_backend(paths, commands)
            backend.write_build_marker()
            base_marker = backend.build_marker.read_text(encoding="utf-8")
            backend.gui_build_marker.write_text(
                json.dumps({"image_version": GUI_IMAGE_VERSION - 1}), encoding="utf-8"
            )
            backend.ensure_image_built(graphics=True)
            backend.ensure_image_built(gui=True)
            self.assertEqual(commands, [backend.build_cmd(gui=True)])
            self.assertEqual(backend.build_marker.read_text(encoding="utf-8"), base_marker)
            marker = json.loads(backend.gui_build_marker.read_text(encoding="utf-8"))
            self.assertEqual(marker["image_version"], GUI_IMAGE_VERSION)

    def test_kvm_create_exposes_device_and_preserves_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            commands = []
            backend = self.make_backend(paths, commands)
            app = AgenticSandbox(paths, root, backend=backend)
            with (
                patch.object(Path, "is_char_device", return_value=True),
                patch(
                    "agentic_sandbox.podman_backend.os.access", return_value=True
                ) as access,
                patch.object(backend, "ensure_workspace"),
                patch.object(backend, "wait_for_container"),
            ):
                app.create(kvm=True, quiet=True)

            access.assert_called_once_with(KVM_DEVICE, os.R_OK | os.W_OK)
            command = next(cmd for cmd in commands if cmd[:2] == ["podman", "create"])
            self.assertEqual(
                command[command.index("--device") + 1], "/dev/kvm:/dev/kvm:rw"
            )
            self.assertEqual(command[command.index("--group-add") + 1], "keep-groups")
            self.assertIn(f"{KVM_LABEL}=enabled", command)
            self.assertLess(command.index("--device"), command.index(backend.image_tag))
            self.assertNotIn("--privileged", command)

    def test_kvm_rejects_unavailable_device_before_building(self) -> None:
        for is_device, accessible, message in (
            (False, True, "host character device"),
            (True, False, "read/write access"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)
                commands = []
                backend = self.make_backend(paths, commands)
                identity = AgenticSandbox(paths, root, backend=backend).identity_for()
                with (
                    patch.object(Path, "is_char_device", return_value=is_device),
                    patch(
                        "agentic_sandbox.podman_backend.os.access",
                        return_value=accessible,
                    ),
                    self.assertRaisesRegex(AgenticSandboxError, message),
                ):
                    backend.create(identity, root, kvm=True)
                self.assertEqual(
                    commands,
                    [["podman", "container", "exists", identity.machine_name]],
                )
                self.assertFalse(paths.podman_build_marker.exists())

    def test_kvm_request_rejects_existing_container_without_kvm(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                paths = self.make_paths(root)
                commands = []
                backend = self.make_backend(paths, commands)
                backend.is_known = lambda identity: True
                backend.is_running = lambda identity, running=running: running
                app = AgenticSandbox(paths, root, backend=backend)
                with self.assertRaisesRegex(
                    AgenticSandboxError, "was created without --kvm"
                ):
                    app.create(kvm=True, quiet=True)
                self.assertTrue(commands)
                self.assertTrue(
                    all(cmd[:3] == ["podman", "container", "inspect"] for cmd in commands)
                )
                self.assertFalse(app.identity_for().state_file.exists())

    def test_kvm_container_is_reused_by_create_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            paths.podman_build_marker.write_text("{}", encoding="utf-8")
            commands = []
            running = False

            def runner(command, **kwargs):
                nonlocal running
                commands.append(command)
                output = ""
                if command[:2] == ["podman", "start"]:
                    running = True
                elif command[:3] == ["podman", "container", "inspect"]:
                    if command[-2] == "{{.State.Running}}":
                        output = str(running).lower()
                    elif KVM_LABEL in command[-2]:
                        output = "enabled"
                return subprocess.CompletedProcess(command, 0, output, "")

            backend = PodmanBackend(paths, runner=runner, spinner_enabled=False)
            backend.should_allocate_tty = lambda: False
            app = AgenticSandbox(paths, root, backend=backend)
            with patch.object(backend, "ensure_workspace"):
                app.create(kvm=True, quiet=True)
                app.create(kvm=True, quiet=True)
                self.assertEqual(app.run_sandbox(["--", "true"], quiet=True), 0)

            self.assertEqual(
                commands.count(["podman", "start", app.identity_for().machine_name]), 1
            )
            self.assertFalse(
                any(cmd[:2] in (["podman", "create"], ["podman", "build"]) for cmd in commands)
            )
            self.assertEqual(commands[-1][:2], ["podman", "exec"])
