import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentic_sandbox.main import AgenticVM, Paths


class StopTests(unittest.TestCase):
    def make_paths(self, root: Path) -> Paths:
        repo_root = root / "repo"
        template_dir = repo_root / "mkosi"
        home = root / "home"
        data_dir = root / "data"
        state_dir = root / "state"
        image_dir = data_dir / "base-image"
        podman_image_dir = data_dir / "podman-image"
        template_dir.mkdir(parents=True)
        (repo_root / "podman").mkdir(parents=True)
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
            podman_template_dir=repo_root / "podman",
            podman_image_dir=podman_image_dir,
            podman_build_marker=podman_image_dir / ".image-built.json",
        )

    def write_state(self, paths: Paths, name: str, cwd: Path) -> Path:
        state_file = paths.state_dir / f"{name}.json"
        state_file.write_text(
            json.dumps(
                {
                    "cwd": str(cwd),
                    "vm_id": name,
                    "unit_name": f"agentic-sandbox-{name}.service",
                    "machine_name": f"agentic-sandbox-{name}",
                    "image_dir": str(paths.image_dir),
                    "backend": "mkosi",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return state_file

    def write_backend_state(
        self, paths: Paths, name: str, cwd: Path, backend: str
    ) -> Path:
        state_file = (
            paths.state_dir / f"{name}.json"
            if backend == "mkosi"
            else paths.state_dir / f"{backend}-{name}.json"
        )
        image_dir = paths.image_dir if backend == "mkosi" else paths.podman_image_dir
        state_file.write_text(
            json.dumps(
                {
                    "cwd": str(cwd),
                    "vm_id": name,
                    "unit_name": f"agentic-sandbox-{name}.service",
                    "machine_name": f"agentic-sandbox-{name}",
                    "image_dir": str(image_dir),
                    "backend": backend,
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

            stop_calls = []

            class FakeBackend:
                def create(self, identity, cwd, wait=False):
                    raise AssertionError("create not expected")

                def ssh(self, identity, cwd, extra_args):
                    raise AssertionError("ssh not expected")

                def stop(
                    self, identity, force, timeout_seconds, poll_interval_seconds
                ):
                    stop_calls.append(
                        (
                            identity.unit_name,
                            force,
                            timeout_seconds,
                            poll_interval_seconds,
                        )
                    )
                    return True

                def rebuild(self):
                    raise AssertionError("rebuild not expected")

                def is_running(self, identity):
                    return True

                def is_known(self, identity):
                    return True

            app = AgenticVM(paths, cwd, backend=FakeBackend())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                app.stop(all_vms=True)

            self.assertEqual(
                stop_calls,
                [
                    ("agentic-sandbox-first.service", False, 30.0, 1.0),
                    ("agentic-sandbox-second.service", False, 30.0, 1.0),
                ],
            )
            self.assertFalse(first_state.exists())
            self.assertFalse(second_state.exists())
            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    "stopped agentic-sandbox-first.service",
                    "stopped agentic-sandbox-second.service",
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

            class FakeBackend:
                def create(self, identity, cwd, wait=False):
                    raise AssertionError("create not expected")

                def ssh(self, identity, cwd, extra_args):
                    raise AssertionError("ssh not expected")

                def stop(
                    self, identity, force, timeout_seconds, poll_interval_seconds
                ):
                    raise AssertionError("stop not expected")

                def rebuild(self):
                    raise AssertionError("rebuild not expected")

                def is_running(self, identity):
                    return False

                def is_known(self, identity):
                    return False

            app = AgenticVM(paths, cwd, backend=FakeBackend())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                app.stop(all_vms=True)

            self.assertFalse(stale_state.exists())
            self.assertEqual(output.getvalue().splitlines(), ["no managed VMs were running"])

    def test_create_delegates_to_backend_and_writes_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            calls = []

            class FakeBackend:
                def create(self, identity, backend_cwd, wait=False):
                    calls.append((identity.unit_name, backend_cwd, wait))

                def ssh(self, identity, backend_cwd, extra_args):
                    raise AssertionError("ssh not expected")

                def stop(
                    self, identity, force, timeout_seconds, poll_interval_seconds
                ):
                    raise AssertionError("stop not expected")

                def rebuild(self):
                    raise AssertionError("rebuild not expected")

                def is_running(self, identity):
                    return False

                def is_known(self, identity):
                    return False

            app = AgenticVM(paths, cwd, backend=FakeBackend())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                app.create(wait=True)

            identity = app.identity_for()
            self.assertEqual(calls, [(identity.unit_name, cwd.resolve(), True)])
            self.assertTrue(identity.state_file.exists())
            self.assertEqual(output.getvalue().splitlines(), [f"created {identity.unit_name}"])

    def test_stop_all_only_uses_selected_backend_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()
            mkosi_cwd = root / "mkosi-project"
            podman_cwd = root / "podman-project"
            mkosi_cwd.mkdir()
            podman_cwd.mkdir()
            mkosi_state = self.write_backend_state(paths, "mkosi", mkosi_cwd, "mkosi")
            podman_state = self.write_backend_state(
                paths, "podman", podman_cwd, "podman"
            )
            stop_calls = []

            class FakeBackend:
                name = "podman"
                image_dir = paths.podman_image_dir

                def create(self, identity, cwd, wait=False):
                    raise AssertionError("create not expected")

                def ssh(self, identity, cwd, extra_args):
                    raise AssertionError("ssh not expected")

                def stop(
                    self, identity, force, timeout_seconds, poll_interval_seconds
                ):
                    stop_calls.append(identity.unit_name)
                    return True

                def rebuild(self):
                    raise AssertionError("rebuild not expected")

                def is_running(self, identity):
                    return True

                def is_known(self, identity):
                    return True

            app = AgenticVM(paths, cwd, backend=FakeBackend())

            with contextlib.redirect_stdout(io.StringIO()):
                app.stop(all_vms=True)

            self.assertEqual(stop_calls, ["agentic-sandbox-podman.service"])
            self.assertTrue(mkosi_state.exists())
            self.assertFalse(podman_state.exists())

    def test_podman_create_and_stop_messages_use_container_name_not_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()

            class FakeBackend:
                name = "podman"
                image_dir = paths.podman_image_dir

                def create(self, identity, backend_cwd, wait=False):
                    return None

                def ssh(self, identity, backend_cwd, extra_args):
                    raise AssertionError("ssh not expected")

                def stop(
                    self, identity, force, timeout_seconds, poll_interval_seconds
                ):
                    return True

                def rebuild(self):
                    raise AssertionError("rebuild not expected")

                def is_running(self, identity):
                    return False

                def is_known(self, identity):
                    return True

            app = AgenticVM(paths, cwd, backend=FakeBackend())
            identity = app.identity_for()
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                app.create()
                app.stop()

            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    f"created {identity.machine_name}",
                    f"stopped {identity.machine_name}",
                ],
            )

    def test_podman_already_running_message_uses_container_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = self.make_paths(root)
            cwd = root / "project"
            cwd.mkdir()

            class FakeBackend:
                name = "podman"
                image_dir = paths.podman_image_dir

                def create(self, identity, backend_cwd, wait=False):
                    raise AssertionError("create not expected")

                def ssh(self, identity, backend_cwd, extra_args):
                    raise AssertionError("ssh not expected")

                def stop(
                    self, identity, force, timeout_seconds, poll_interval_seconds
                ):
                    raise AssertionError("stop not expected")

                def rebuild(self):
                    raise AssertionError("rebuild not expected")

                def is_running(self, identity):
                    return True

                def is_known(self, identity):
                    return True

            app = AgenticVM(paths, cwd, backend=FakeBackend())
            identity = app.identity_for()
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                app.create()

            self.assertEqual(
                output.getvalue().splitlines(),
                [f"{identity.machine_name} is already running"],
            )


if __name__ == "__main__":
    unittest.main()
