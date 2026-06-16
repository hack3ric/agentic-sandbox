from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agentic_vm import APP_NAME, AgenticVM, AgenticVMError, Paths


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    class Result:
        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return Result()


class AgenticVMTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        home = root / "home"
        cwd = root / "project"
        home.mkdir()
        cwd.mkdir()
        self.cwd = cwd
        self.paths = Paths(
            home=home,
            data_dir=home / ".local" / "share" / APP_NAME,
            state_dir=home / ".local" / "state" / APP_NAME,
            image_dir=home / ".local" / "share" / APP_NAME / "base-image",
            config_file=home / ".local" / "share" / APP_NAME / "base-image" / "mkosi.conf",
            build_marker=home / ".local" / "share" / APP_NAME / "base-image" / ".image-built.json",
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_identity_is_stable_for_same_path(self):
        app = AgenticVM(self.paths, self.cwd, runner=Mock())
        first = app.identity_for(self.cwd)
        second = app.identity_for(self.cwd)
        self.assertEqual(first.vm_id, second.vm_id)
        self.assertEqual(first.unit_name, second.unit_name)
        self.assertEqual(first.machine_name, second.machine_name)

    def test_start_builds_then_runs_transient_unit(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="inactive\n", returncode=3)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.start()

        self.assertTrue(self.paths.config_file.exists())
        self.assertEqual(calls[0][0], ["mkosi", "--directory", str(self.paths.image_dir), "genkey"])
        self.assertEqual(calls[1][0], ["systemctl", "--user", "is-active", app.identity_for().unit_name])
        self.assertEqual(calls[2][0], ["mkosi", "--directory", str(self.paths.image_dir), "build"])
        self.assertEqual(
            calls[3][0],
            [
                "systemd-run",
                "--user",
                "--unit",
                app.identity_for().unit_name,
                "--description",
                f"{APP_NAME} VM for {self.cwd.resolve()}",
                "--collect",
                "--service-type=exec",
                "--working-directory",
                str(self.paths.image_dir),
                "mkosi",
                "--directory",
                str(self.paths.image_dir),
                "--vmm=qemu",
                "--machine",
                app.identity_for().machine_name,
                "--ephemeral=yes",
                "--console=read-only",
                "--runtime-network=user",
                "--runtime-tree",
                f"{self.cwd.resolve()}:{self.cwd.resolve()}",
                "--register=no",
                "vm",
            ],
        )
        state = json.loads(app.identity_for().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["cwd"], str(self.cwd.resolve()))

    def test_start_is_idempotent_when_active(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="active\n")
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.start()
        self.assertEqual(calls[0][0], ["mkosi", "--directory", str(self.paths.image_dir), "genkey"])
        self.assertEqual(calls[1][0], ["systemctl", "--user", "is-active", app.identity_for().unit_name])
        self.assertEqual(len(calls), 2)

    def test_ssh_requires_active_unit(self):
        def runner(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="inactive\n", returncode=3)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        with self.assertRaises(AgenticVMError):
            app.ssh([])

    def test_ssh_passes_extra_arguments(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="active\n")
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.ssh(["--", "uname", "-a"])
        self.assertEqual(
            calls[1][0],
            [
                "mkosi",
                "--directory",
                str(self.paths.image_dir),
                "--machine",
                app.identity_for().machine_name,
                "ssh",
                "--",
                "uname",
                "-a",
            ],
        )

    def test_stop_removes_metadata_and_resets_unit(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:4] == ["systemctl", "--user", "status", app.identity_for().unit_name]:
                return completed(stdout="Loaded: loaded\n")
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.ensure_directories()
        app.write_state(app.identity_for())
        app.stop()
        self.assertFalse(app.identity_for().state_file.exists())
        self.assertEqual(calls[1][0], ["systemctl", "--user", "stop", app.identity_for().unit_name])
        self.assertEqual(calls[2][0], ["systemctl", "--user", "reset-failed", app.identity_for().unit_name])

    def test_rebuild_refuses_when_managed_unit_is_active(self):
        app = AgenticVM(self.paths, self.cwd, runner=Mock())
        app.ensure_directories()
        app.write_state(app.identity_for())

        def runner(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="active\n")
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        with self.assertRaises(AgenticVMError):
            app.rebuild()

    def test_rebuild_forces_build_when_inactive(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="inactive\n", returncode=3)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.rebuild()
        self.assertEqual(calls[0][0], ["mkosi", "--directory", str(self.paths.image_dir), "genkey"])
        self.assertEqual(calls[1][0], ["mkosi", "--directory", str(self.paths.image_dir), "-f", "build"])

    def test_rebuild_rewrites_mkosi_config(self):
        def runner(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="inactive\n", returncode=3)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.ensure_directories()
        self.paths.config_file.write_text("stale\n", encoding="utf-8")

        app.rebuild()

        self.assertEqual(
            self.paths.config_file.read_text(encoding="utf-8"),
            app.default_mkosi_config(),
        )


if __name__ == "__main__":
    unittest.main()
