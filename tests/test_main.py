import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sandbox.main import AgenticSandbox, Paths, build_parser, main


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

    def test_parser_defaults_backend_to_mkosi(self) -> None:
        args = build_parser().parse_args(["create"])
        self.assertEqual(args.backend, "mkosi")
        self.assertEqual(args.command, "create")

    def test_parser_accepts_backend_before_subcommand(self) -> None:
        args = build_parser().parse_args(["--backend", "podman", "run", "--", "uname"])
        self.assertEqual(args.backend, "podman")
        self.assertEqual(args.command, "run")
        self.assertEqual(args.ssh_args, ["--", "uname"])

    def test_main_dispatches_selected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self.make_paths(Path(tmpdir))
            selected = []

            class FakeApp:
                def __init__(self, paths_arg, cwd_arg, backend):
                    self.paths_arg = paths_arg
                    self.cwd_arg = cwd_arg
                    self.backend = backend

                def create(self, wait=False):
                    selected.append((self.backend, wait))

            with patch("agentic_sandbox.main.Paths.detect", return_value=paths):
                with patch("agentic_sandbox.main.make_backend", return_value="podman-backend"):
                    with patch("agentic_sandbox.main.AgenticSandbox", FakeApp):
                        result = main(["--backend", "podman", "create", "--wait"])

            self.assertEqual(result, 0)
            self.assertEqual(selected, [("podman-backend", True)])
