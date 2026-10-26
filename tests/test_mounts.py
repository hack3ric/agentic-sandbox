import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sandbox.main import AgenticSandbox, AgenticSandboxError, build_parser, main
from agentic_sandbox.mkosi_backend import GUEST_WORK_MOUNT, MkosiBackend
from agentic_sandbox.mounts import DirectoryMount, parse_mounts
from agentic_sandbox.podman_backend import GUI_RUNTIME_DIR, PodmanBackend

from tests.helpers import make_paths


class MountTests(unittest.TestCase):
    def make_app(
        self, root: Path, backend_name: str, *, running=False, known=False
    ):
        paths = make_paths(root)
        cwd = root / "project"
        cwd.mkdir()
        commands = []
        runtime = {"running": running, "known": known}

        def runner(command, **kwargs):
            commands.append(command)
            output = ""
            status = 0
            if command[:3] == ["systemctl", "--user", "is-active"]:
                status = 0 if runtime["running"] else 3
                output = "active" if runtime["running"] else "inactive"
            elif command[0] == "systemd-run":
                runtime.update(running=True, known=True)
            elif command[:3] == ["podman", "container", "exists"]:
                status = 0 if runtime["known"] else 1
            elif command[:3] == ["podman", "container", "inspect"]:
                if command[-2] == "{{.State.Running}}":
                    output = str(runtime["running"]).lower()
            elif command[:2] == ["podman", "create"]:
                runtime["known"] = True
            elif command[:2] == ["podman", "start"]:
                runtime["running"] = True
            elif command[:2] == ["podman", "rm"] or command[:3] == [
                "systemctl", "--user", "stop"
            ]:
                runtime.update(running=False, known=False)
            return subprocess.CompletedProcess(command, status, output, "")

        backend_class = MkosiBackend if backend_name == "mkosi" else PodmanBackend
        backend = backend_class(paths, runner=runner, error_type=AgenticSandboxError)
        if backend_name == "mkosi":
            backend.ensure_mkosi_workspace = lambda force=False: None
            backend.ensure_ssh_credentials = lambda: None
            backend.ensure_image_built = lambda: commands.append(["build"])
            backend.wait_for_machine = lambda identity: commands.append(["wait"])
        else:
            backend.ensure_workspace = lambda force=False: None
            backend.ensure_image_built = lambda **kwargs: commands.append(["build"])
            backend.wait_for_container = lambda identity: commands.append(["wait"])
        return AgenticSandbox(paths, cwd, backend=backend), commands, runtime

    def test_mount_is_repeatable_and_only_parsed_before_separator(self) -> None:
        for backend in ("mkosi", "podman"):
            with self.subTest(backend=backend):
                args = build_parser().parse_args([
                    "-b", backend, "create", "-m", "../shared",
                    "--mount", "~/data:/data",
                    "--", "--mount", "backend-option",
                ])
                self.assertEqual(args.mounts, ["../shared", "~/data:/data"])
                self.assertEqual(args.backend_args, ["--", "--mount", "backend-option"])
                self.assertEqual(
                    build_parser().parse_args(["-b", backend, "create"]).mounts, []
                )
        for command in ("run", "exec", "stop", "rebuild"):
            with self.subTest(command=command):
                with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args(
                        ["-b", "podman", command, "--mount", "/data"]
                    )
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["-b", "podman", "create", "--mount"])

    def test_main_passes_mounts_to_create(self) -> None:
        with (
            patch("agentic_sandbox.main.Paths.detect"),
            patch("agentic_sandbox.main.make_backend"),
            patch("agentic_sandbox.main.AgenticSandbox.create") as create,
        ):
            self.assertEqual(main([
                "-b", "podman", "create", "-m", "../shared", "--mount", "~/data:/data",
            ]), 0)
        self.assertEqual(
            create.call_args.kwargs["mounts"], ["../shared", "~/data:/data"]
        )

    def test_normalizes_paths_without_resolving_guest_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "project"
            cwd.mkdir()
            source = root / "shared files"
            source.mkdir()
            (root / "link").symlink_to(source, target_is_directory=True)
            guest = root / "guest-link"
            guest.symlink_to(source, target_is_directory=True)
            with patch.dict(os.environ, {"HOME": str(root)}):
                mounts = parse_mounts([
                    "../link", "~/shared files:/data/../datasets/", f"../link:{guest}",
                    "../shared files",
                ], cwd, [cwd])
            self.assertEqual(mounts, [
                DirectoryMount(source, source),
                DirectoryMount(source, Path("/datasets")),
                DirectoryMount(source, guest),
            ])

    def test_invalid_mounts_fail_before_lifecycle_operations(self) -> None:
        for backend in ("mkosi", "podman"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                app, commands, runtime = self.make_app(root, backend)
                source = root / "shared"
                source.mkdir()
                regular_file = root / "file"
                regular_file.touch()
                colon_source = root / "colon:name"
                colon_source.mkdir()
                (root / "link").symlink_to(colon_source, target_is_directory=True)
                internal = GUEST_WORK_MOUNT if backend == "mkosi" else GUI_RUNTIME_DIR
                invalid = [
                    [""], [":/data"], [f"{source}:"], [f"{source}:/data:ro"],
                    [str(root / "missing")], [str(regular_file)],
                    [f"{source}:relative"], [f"{source}:~/data"],
                    [f"{source}:/data\nother"], [str(root / "link")],
                    [f"{source}:/data", f"{root}:/data"],
                    [f"{source}:/data", f"{root}:/data/child"],
                    [f"{source}:/data/child", f"{root}:/data"],
                    [f"{source}:{app.cwd}"], [f"{source}:{app.cwd}/child"],
                    [f"{source}:{app.cwd.parent}"], [f"{source}:/"],
                    [f"{source}:/root/.codex"], [f"{source}:/root/.codex/subdir"],
                    [f"{source}:/root"],
                    [f"{source}:{internal}"],
                ]
                for specs in invalid:
                    with self.subTest(specs=specs):
                        with self.assertRaises(AgenticSandboxError), patch.object(
                            app, "ensure_directories"
                        ) as setup:
                            app.create(mounts=specs, quiet=True)
                        setup.assert_not_called()
                        self.assertEqual(commands, [])
                        self.assertFalse(runtime["known"])
                        self.assertFalse(app.identity_for().state_file.exists())

    def test_main_reports_mount_errors_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = make_paths(Path(tmpdir))
            with (
                patch("agentic_sandbox.main.Paths.detect", return_value=paths),
                patch("subprocess.run") as runner,
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main(["-b", "mkosi", "create", "--mount", ":/data"]), 1
                )
            runner.assert_not_called()
            self.assertIn("expected HOST[:GUEST]", stderr.getvalue())

    def test_create_passes_mounts_to_backends_and_records_configuration(self) -> None:
        for backend in ("mkosi", "podman"):
            for wait in (False, True):
                with (
                    self.subTest(backend=backend, wait=wait),
                    tempfile.TemporaryDirectory() as tmpdir,
                ):
                    root = Path(tmpdir)
                    app, commands, runtime = self.make_app(root, backend)
                    source = root / "shared files"
                    source.mkdir()
                    (app.paths.home / ".codex").mkdir()
                    expected = [f"{source}:{source}", f"{source}:/datasets"]
                    app.create(
                        mounts=["../shared files", f"{source}:/datasets"],
                        backend_args=["--", "--example-option", "value"],
                        wait=wait, quiet=True,
                    )
                    prefix = (
                        ["systemd-run"] if backend == "mkosi" else ["podman", "create"]
                    )
                    command = next(
                        cmd for cmd in commands if cmd[:len(prefix)] == prefix
                    )
                    option = "--runtime-tree" if backend == "mkosi" else "--volume"
                    values = [
                        command[i + 1]
                        for i, value in enumerate(command) if value == option
                    ]
                    self.assertEqual(values[-2:], expected)
                    self.assertIn(f"{app.paths.home / '.codex'}:/root/.codex", values)
                    target = GUEST_WORK_MOUNT if backend == "mkosi" else app.cwd
                    self.assertIn(f"{app.cwd}:{target}", values)
                    end = "vm" if backend == "mkosi" else app.backend.image_tag
                    self.assertLess(
                        command.index("--example-option"), command.index(end)
                    )
                    if backend == "podman":
                        self.assertLess(command.index(expected[-1]), command.index(end))
                    self.assertTrue(runtime["running"])
                    self.assertEqual(["wait"] in commands, wait)
                    state = json.loads(app.identity_for().state_file.read_text())
                    self.assertEqual(state["mounts"], expected)

    def test_create_and_run_preserve_existing_mounts(self) -> None:
        for backend in ("mkosi", "podman"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                app, commands, _ = self.make_app(root, backend)
                source = root / "shared"
                source.mkdir()
                specs = [f"{source}:/first", f"{source}:/second"]
                app.create(mounts=specs, quiet=True)
                commands.clear()
                with self.assertRaisesRegex(AgenticSandboxError, "stop and recreate"):
                    app.create(mounts=specs[:1], quiet=True)
                app.create(mounts=list(reversed(specs)), quiet=True)
                app.create(quiet=True)
                self.assertEqual(app.run_sandbox(["--", "true"], quiet=True), 0)
                self.assertNotIn(["build"], commands)
                self.assertFalse(any(cmd[0] == "systemd-run" or cmd[:2] == [
                    "podman", "create"
                ] for cmd in commands))
                state = json.loads(app.identity_for().state_file.read_text())
                self.assertEqual(state["mounts"], specs)

    def test_existing_sandbox_rejects_changed_or_unrecorded_mounts(self) -> None:
        for backend, running in (("mkosi", True), ("podman", True), ("podman", False)):
            for state in ({}, {"mounts": []}, {"mounts": ["/old:/data"]},
                          {"mounts": None}, {"mounts": [{}]}, [], "invalid", None):
                with (
                    self.subTest(backend=backend, running=running, state=state),
                    tempfile.TemporaryDirectory() as tmpdir,
                ):
                    root = Path(tmpdir)
                    app, commands, _ = self.make_app(
                        root, backend, running=running, known=True
                    )
                    source = root / "shared"
                    source.mkdir()
                    state_file = app.identity_for().state_file
                    if state is not None:
                        state_file.write_text(
                            state if isinstance(state, str) else json.dumps(state)
                        )
                    before = state_file.read_bytes() if state_file.exists() else None
                    with self.assertRaisesRegex(
                        AgenticSandboxError, "stop and recreate"
                    ):
                        app.create(mounts=[f"{source}:/data"], quiet=True)
                    self.assertEqual(
                        state_file.read_bytes() if state_file.exists() else None, before
                    )
                    self.assertNotIn(["wait"], commands)
                    self.assertFalse(any(cmd[0] == "systemd-run" or cmd[:2] in (
                        ["podman", "build"], ["podman", "create"], ["podman", "start"]
                    ) for cmd in commands))

    def test_stopped_container_preserves_mounts_when_restarted(self) -> None:
        for repeat_mounts in (False, True):
            with (
                self.subTest(repeat_mounts=repeat_mounts),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                app, commands, runtime = self.make_app(root, "podman")
                source = root / "shared"
                source.mkdir()
                specs = [f"{source}:/data"]
                app.create(mounts=specs, quiet=True)
                runtime["running"] = False
                commands.clear()
                if repeat_mounts:
                    app.create(mounts=specs, quiet=True)
                else:
                    app.run_sandbox(["--", "true"], quiet=True)
                self.assertIn(
                    ["podman", "start", app.identity_for().machine_name], commands
                )
                self.assertNotIn(["build"], commands)
                state = json.loads(app.identity_for().state_file.read_text())
                self.assertEqual(state["mounts"], specs)

    def test_new_sandbox_does_not_inherit_stale_mounts(self) -> None:
        for backend in ("mkosi", "podman"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmpdir:
                app, _, _ = self.make_app(Path(tmpdir), backend)
                state_file = app.identity_for().state_file
                state_file.write_text(json.dumps({"mounts": ["/old:/data"]}))
                app.create(quiet=True)
                self.assertEqual(json.loads(state_file.read_text())["mounts"], [])

    def test_failed_create_does_not_record_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app, _, _ = self.make_app(root, "mkosi")
            source = root / "shared"
            source.mkdir()
            with patch.object(
                app.backend, "create", side_effect=AgenticSandboxError("failed")
            ):
                with self.assertRaisesRegex(AgenticSandboxError, "failed"):
                    app.create(mounts=[f"{source}:/data"], quiet=True)
            self.assertFalse(app.identity_for().state_file.exists())

    def test_readiness_timeout_preserves_mounts_for_retry(self) -> None:
        for backend in ("mkosi", "podman"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                app, commands, _ = self.make_app(root, backend)
                source = root / "shared"
                source.mkdir()
                specs = [f"{source}:/data"]
                with patch.object(
                    app.backend, "wait_until_ready",
                    side_effect=AgenticSandboxError("timed out"),
                ):
                    with self.assertRaisesRegex(AgenticSandboxError, "timed out"):
                        app.create(mounts=specs, quiet=True)
                state = json.loads(app.identity_for().state_file.read_text())
                self.assertEqual(state["mounts"], specs)
                commands.clear()
                app.create(mounts=specs, quiet=True)
                self.assertNotIn(["build"], commands)
                self.assertEqual(commands[-1], ["wait"])

    def test_stop_clears_mounts_and_allows_recreation_with_new_mounts(self) -> None:
        for backend in ("mkosi", "podman"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                app, _, _ = self.make_app(root, backend)
                source = root / "shared"
                source.mkdir()
                app.create(mounts=[f"{source}:/old"], quiet=True)
                with patch("sys.stdout", io.StringIO()):
                    app.stop(force=True)
                self.assertFalse(app.identity_for().state_file.exists())
                specs = [f"{source}:/new"]
                app.create(mounts=specs, quiet=True)
                state = json.loads(app.identity_for().state_file.read_text())
                self.assertEqual(state["mounts"], specs)


if __name__ == "__main__":
    unittest.main()
