import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentic_vm.main import AgenticVM, Paths


class StopTests(unittest.TestCase):
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

    def write_state(self, paths: Paths, name: str, cwd: Path) -> Path:
        state_file = paths.state_dir / f"{name}.json"
        state_file.write_text(
            json.dumps(
                {
                    "cwd": str(cwd),
                    "vm_id": name,
                    "unit_name": f"agentic-vm-{name}.service",
                    "machine_name": f"agentic-vm-{name}",
                    "image_dir": str(paths.image_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return state_file

    def test_stop_all_stops_each_known_managed_vm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            first_cwd = root / "first"
            second_cwd = root / "second"
            first_cwd.mkdir()
            second_cwd.mkdir()
            first_state = self.write_state(paths, "first", first_cwd)
            second_state = self.write_state(paths, "second", second_cwd)

            graceful_stops = []
            waited_for = []

            app = AgenticVM(paths, cwd, spinner_enabled=False)
            app.unit_known = lambda unit_name: True
            app.request_graceful_stop = lambda identity: graceful_stops.append(
                identity.unit_name
            )
            app.wait_for_unit_inactive = (
                lambda identity, timeout_seconds, poll_interval_seconds: (
                    waited_for.append(identity.unit_name) or True
                )
            )
            app.force_stop_unit = lambda identity: self.fail("force stop not expected")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                app.stop(all_vms=True)

            self.assertEqual(
                graceful_stops,
                ["agentic-vm-first.service", "agentic-vm-second.service"],
            )
            self.assertEqual(waited_for, graceful_stops)
            self.assertFalse(first_state.exists())
            self.assertFalse(second_state.exists())
            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    "stopped agentic-vm-first.service",
                    "stopped agentic-vm-second.service",
                ],
            )

    def test_stop_all_prunes_stale_state_and_reports_no_running_vms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            stale_cwd = root / "stale"
            stale_cwd.mkdir()
            stale_state = self.write_state(paths, "stale", stale_cwd)

            app = AgenticVM(paths, cwd, spinner_enabled=False)
            app.unit_known = lambda unit_name: False
            app.request_graceful_stop = lambda identity: self.fail(
                "graceful stop not expected"
            )
            app.wait_for_unit_inactive = lambda *args, **kwargs: self.fail(
                "wait not expected"
            )
            app.force_stop_unit = lambda identity: self.fail("force stop not expected")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                app.stop(all_vms=True)

            self.assertFalse(stale_state.exists())
            self.assertEqual(output.getvalue().splitlines(), ["no managed VMs were running"])


if __name__ == "__main__":
    unittest.main()
