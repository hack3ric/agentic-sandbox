import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sandbox.main import AgenticSandboxError, Paths, build_parser, main


class MainTests(unittest.TestCase):
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

    def test_parser_requires_backend(self) -> None:
        for command in ("create", "run", "exec", "stop", "rebuild"):
            with self.subTest(command=command):
                stderr = io.StringIO()
                with (
                    patch("sys.stderr", stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    build_parser().parse_args([command])

                self.assertEqual(raised.exception.code, 2)
                self.assertIn("required: -b/--backend", stderr.getvalue())

    def test_parser_accepts_explicit_backends(self) -> None:
        for option in ("--backend", "-b"):
            for backend in ("mkosi", "podman"):
                with self.subTest(option=option, backend=backend):
                    args = build_parser().parse_args([option, backend, "create"])
                    self.assertEqual(args.backend, backend)
                    self.assertEqual(args.command, "create")

    def test_parser_accepts_backend_before_subcommand(self) -> None:
        args = build_parser().parse_args(["--backend", "podman", "run", "--", "uname"])
        self.assertEqual(args.backend, "podman")
        self.assertEqual(args.command, "run")
        self.assertEqual(args.exec_args, ["--", "uname"])

    def test_create_waits_by_default_and_accepts_no_wait(self) -> None:
        for backend in ("mkosi", "podman"):
            for flags, expected in (
                ([], True), (["--wait"], True), (["--no-wait"], False)
            ):
                with self.subTest(backend=backend, flags=flags):
                    args = build_parser().parse_args(["-b", backend, "create", *flags])
                    self.assertEqual(args.wait, expected)
            args = build_parser().parse_args(
                ["-b", backend, "create", "--", "--no-wait"]
            )
            self.assertTrue(args.wait)
            self.assertEqual(args.backend_args, ["--", "--no-wait"])

    def test_no_wait_skips_readiness_for_running_sandbox(self) -> None:
        for backend_name in ("mkosi", "podman"):
            with (
                self.subTest(backend=backend_name),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                paths = self.make_paths(Path(tmpdir))

                class FakeBackend:
                    name = backend_name

                    def is_running(self, identity):
                        return True

                    def wait_until_ready(self, identity):
                        raise AssertionError("waiting not expected")

                with (
                    patch("agentic_sandbox.main.Paths.detect", return_value=paths),
                    patch(
                        "agentic_sandbox.main.make_backend", return_value=FakeBackend()
                    ),
                ):
                    self.assertEqual(
                        main(["-b", backend_name, "create", "--no-wait", "-q"]), 0
                    )

    def test_parser_collects_create_backend_args_after_separator(self) -> None:
        args = build_parser().parse_args(
            ["--backend", "podman", "create", "--wait", "--", "--cpus", "2"]
        )

        self.assertEqual(args.backend, "podman")
        self.assertTrue(args.wait)
        self.assertEqual(args.backend_args, ["--", "--cpus", "2"])

    def test_gui_is_a_create_option_only(self) -> None:
        args = build_parser().parse_args(
            ["--backend", "podman", "create", "--gui", "--wait"]
        )
        self.assertTrue(args.gui)
        self.assertTrue(args.wait)
        for command in ("run", "exec"):
            with self.subTest(command=command):
                with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                    build_parser().parse_args(["-b", "podman", command, "--gui"])
                args = build_parser().parse_args(
                    ["-b", "podman", command, "--", "example", "--gui"]
                )
                self.assertEqual(args.exec_args, ["--", "example", "--gui"])

    def test_gui_rejects_mkosi_before_calling_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = io.StringIO()
            with (
                patch(
                    "agentic_sandbox.main.Paths.detect",
                    return_value=self.make_paths(Path(tmpdir)),
                ),
                patch("agentic_sandbox.mkosi_backend.MkosiBackend.create") as create,
                patch("sys.stderr", stderr),
            ):
                result = main(["-b", "mkosi", "create", "--gui"])
            self.assertEqual(result, 1)
            create.assert_not_called()
            self.assertIn(
                "--gui is only supported by the podman backend", stderr.getvalue()
            )

    def test_main_passes_gui_to_create(self) -> None:
        with (
            patch("agentic_sandbox.main.Paths.detect"),
            patch("agentic_sandbox.main.make_backend"),
            patch("agentic_sandbox.main.AgenticSandbox.create") as create,
        ):
            result = main(["-b", "podman", "create", "--gui", "--wait", "-q"])
        self.assertEqual(result, 0)
        create.assert_called_once_with(
            wait=True, backend_args=[], quiet=True, gui=True, kvm=False
        )

    def test_kvm_is_a_create_option_only(self) -> None:
        with (
            patch("agentic_sandbox.main.Paths.detect"),
            patch("agentic_sandbox.main.make_backend"),
            patch("agentic_sandbox.main.AgenticSandbox.create") as create,
        ):
            self.assertEqual(main(["-b", "podman", "create", "--kvm"]), 0)
        create.assert_called_once_with(
            wait=True, backend_args=[], quiet=False, gui=False, kvm=True
        )
        self.assertFalse(build_parser().parse_args(["-b", "podman", "create"]).kvm)
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["-b", "podman", "run", "--kvm"])

    def test_kvm_rejects_mkosi_before_calling_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = io.StringIO()
            with (
                patch(
                    "agentic_sandbox.main.Paths.detect",
                    return_value=self.make_paths(Path(tmpdir)),
                ),
                patch("agentic_sandbox.mkosi_backend.MkosiBackend.create") as create,
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main(["-b", "mkosi", "create", "--kvm"]), 1)
            create.assert_not_called()
            self.assertIn(
                "--kvm is only supported by the podman backend", stderr.getvalue()
            )

    def test_parser_forwards_quiet_flags_after_separator(self) -> None:
        for command, attribute in (("create", "backend_args"), ("run", "exec_args")):
            with self.subTest(command=command):
                args = build_parser().parse_args(
                    ["--backend", "podman", command, "--", "-q", "--quiet"]
                )

                self.assertFalse(args.quiet)
                self.assertEqual(getattr(args, attribute), ["--", "-q", "--quiet"])

    def test_quiet_suppresses_status_and_preserves_command_output(self) -> None:
        for command in ("create", "run"):
            for running in (False, True):
                for quiet_args in ([], ["-q"], ["--quiet"]):
                    with (
                        self.subTest(
                            command=command, running=running, flags=quiet_args
                        ),
                        tempfile.TemporaryDirectory() as tmpdir,
                    ):
                        paths = self.make_paths(Path(tmpdir))
                        calls = []

                        class FakeBackend:
                            name = "podman"
                            active = running

                            def is_running(self, identity):
                                return self.active

                            def create(
                                self, identity, cwd, wait=False, backend_args=()
                            ):
                                calls.append(("create", wait))
                                self.active = True

                            def wait_until_ready(self, identity):
                                calls.append(("wait",))

                            def ssh(self, identity, cwd, extra_args):
                                calls.append(("exec", list(extra_args)))
                                print("command output")
                                print("command error", file=sys.stderr)
                                return 7

                        stdout = io.StringIO()
                        stderr = io.StringIO()
                        forwarded = (
                            ["--", "example", "-q"] if command == "run" else []
                        )
                        with (
                            patch(
                                "agentic_sandbox.main.Paths.detect", return_value=paths
                            ),
                            patch(
                                "agentic_sandbox.main.make_backend",
                                return_value=FakeBackend(),
                            ),
                            patch("sys.stdout", stdout),
                            patch("sys.stderr", stderr),
                        ):
                            result = main(
                                [
                                    "--backend", "podman", command,
                                    *quiet_args, *forwarded,
                                ]
                            )

                        expected_calls = (
                            [("wait",)] if running else [("create", True)]
                        )
                        expected_output = (
                            "command output\n" if command == "run" else ""
                        )
                        if command == "run":
                            expected_calls.append(("exec", forwarded))
                        self.assertEqual(calls, expected_calls)
                        self.assertEqual(result, 7 if command == "run" else 0)
                        self.assertEqual(
                            stderr.getvalue(),
                            "command error\n" if command == "run" else "",
                        )
                        self.assertEqual(
                            len(list(paths.state_dir.glob("*.json"))), 1
                        )
                        if quiet_args:
                            self.assertEqual(stdout.getvalue(), expected_output)
                        else:
                            lines = stdout.getvalue().splitlines(keepends=True)
                            self.assertIn(
                                "already running" if running else "created", lines[0]
                            )
                            self.assertEqual("".join(lines[1:]), expected_output)

    def test_quiet_preserves_sandbox_errors(self) -> None:
        for command in ("create", "run"):
            with self.subTest(command=command):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch("agentic_sandbox.main.Paths.detect"),
                    patch("agentic_sandbox.main.make_backend"),
                    patch(
                        "agentic_sandbox.main.AgenticSandbox.create",
                        side_effect=AgenticSandboxError("startup failed"),
                    ),
                    patch("sys.stdout", stdout),
                    patch("sys.stderr", stderr),
                ):
                    result = main(["--backend", "podman", command, "-q"])

                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(), "agentic-sandbox: startup failed\n"
                )

    def test_main_dispatches_selected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self.make_paths(Path(tmpdir))
            selected = []

            class FakeApp:
                def __init__(self, paths_arg, cwd_arg, backend):
                    self.paths_arg = paths_arg
                    self.cwd_arg = cwd_arg
                    self.backend = backend

                def create(
                    self, wait=False, backend_args=(), quiet=False, gui=False, kvm=False
                ):
                    selected.append((self.backend, wait, list(backend_args)))

            with (
                patch("agentic_sandbox.main.Paths.detect", return_value=paths),
                patch(
                    "agentic_sandbox.main.make_backend", return_value="podman-backend"
                ),
                patch("agentic_sandbox.main.AgenticSandbox", FakeApp),
            ):
                result = main(
                    [
                        "--backend",
                        "podman",
                        "create",
                        "--wait",
                        "--",
                        "--cpus",
                        "2",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                selected, [("podman-backend", True, ["--", "--cpus", "2"])]
            )
