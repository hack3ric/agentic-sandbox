from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agentic_vm import (
    APP_NAME,
    DEFAULT_PACKAGES,
    DEFAULT_RUNTIME_SIZE,
    AgenticVM,
    AgenticVMError,
    Paths,
)


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
            repo_root=root / "repo",
            template_dir=root / "repo" / "mkosi",
            home=home,
            data_dir=home / ".local" / "share" / APP_NAME,
            state_dir=home / ".local" / "state" / APP_NAME,
            image_dir=home / ".local" / "share" / APP_NAME / "base-image",
            config_file=home
            / ".local"
            / "share"
            / APP_NAME
            / "base-image"
            / "mkosi.conf",
            repart_dir=home
            / ".local"
            / "share"
            / APP_NAME
            / "base-image"
            / "mkosi.repart",
            root_partition_file=home
            / ".local"
            / "share"
            / APP_NAME
            / "base-image"
            / "mkosi.repart"
            / "10-root.conf",
            build_marker=home
            / ".local"
            / "share"
            / APP_NAME
            / "base-image"
            / ".image-built.json",
        )
        self.paths.template_dir.mkdir(parents=True)
        (self.paths.template_dir / "mkosi.conf.in").write_text(
            "[Distribution]\nDistribution=arch\n\n[Output]\nFormat=disk\n\n[Content]\nPackages=@PACKAGES@\nBootable=yes\n",
            encoding="utf-8",
        )
        (self.paths.template_dir / "mkosi.repart").mkdir()
        (self.paths.template_dir / "mkosi.repart" / "10-root.conf").write_text(
            "[Partition]\nType=root\nFormat=btrfs\nCopyFiles=/\nMinimize=guess\n",
            encoding="utf-8",
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

    def test_create_builds_then_runs_transient_unit(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="inactive\n", returncode=3)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.create()

        self.assertTrue(self.paths.config_file.exists())
        self.assertTrue(self.paths.root_partition_file.exists())
        self.assertEqual(
            calls[0][0], ["mkosi", "--directory", str(self.paths.image_dir), "genkey"]
        )
        self.assertEqual(
            calls[1][0],
            ["systemctl", "--user", "is-active", app.identity_for().unit_name],
        )
        self.assertEqual(
            calls[2][0], ["mkosi", "--directory", str(self.paths.image_dir), "build"]
        )
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
                "--runtime-size",
                DEFAULT_RUNTIME_SIZE,
                "--runtime-network=user",
                "--runtime-tree",
                f"{self.cwd.resolve()}:{self.cwd.resolve()}",
                "--register=no",
                "vm",
            ],
        )
        state = json.loads(app.identity_for().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["cwd"], str(self.cwd.resolve()))

    def test_create_is_idempotent_when_active(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="active\n")
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.create()
        self.assertEqual(
            calls[0][0], ["mkosi", "--directory", str(self.paths.image_dir), "genkey"]
        )
        self.assertEqual(
            calls[1][0],
            ["systemctl", "--user", "is-active", app.identity_for().unit_name],
        )
        self.assertEqual(len(calls), 2)

    def test_create_waits_for_machine_when_requested(self):
        calls = []
        ssh_probe_attempts = 0
        sleeps = []

        def runner(cmd, **kwargs):
            nonlocal ssh_probe_attempts
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                checks = len([call for call, _ in calls if call[:3] == cmd[:3]])
                if checks == 1:
                    return completed(stdout="inactive\n", returncode=3)
                return completed(stdout="active\n")
            if cmd[:5] == [
                "mkosi",
                "--directory",
                str(self.paths.image_dir),
                "--machine",
                app.identity_for().machine_name,
            ] and cmd[-2:] == ["--", "true"]:
                ssh_probe_attempts += 1
                if ssh_probe_attempts == 1:
                    return completed(returncode=255)
                return completed()
            return completed()

        app = AgenticVM(
            self.paths,
            self.cwd,
            runner=runner,
            sleeper=lambda seconds: sleeps.append(seconds),
        )
        app.create(wait=True)

        self.assertEqual(sleeps, [1.0])
        self.assertEqual(ssh_probe_attempts, 2)

    def test_run_creates_then_sshes(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                checks = len([call for call, _ in calls if call[:3] == cmd[:3]])
                if checks == 1:
                    return completed(stdout="inactive\n", returncode=3)
                return completed(stdout="active\n")
            if cmd[:5] == [
                "mkosi",
                "--directory",
                str(self.paths.image_dir),
                "--machine",
                app.identity_for().machine_name,
            ] and cmd[-2:] == ["--", "true"]:
                return completed()
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner, sleeper=lambda _: None)
        result = app.run_vm(["--", "uname", "-a"])

        self.assertEqual(result, 0)
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
                "--runtime-size",
                DEFAULT_RUNTIME_SIZE,
                "--runtime-network=user",
                "--runtime-tree",
                f"{self.cwd.resolve()}:{self.cwd.resolve()}",
                "--register=no",
                "vm",
            ],
        )
        self.assertEqual(
            calls[-1][0],
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

    def test_run_waits_for_machine_to_become_reachable(self):
        calls = []
        ssh_probe_attempts = 0
        sleeps = []

        def runner(cmd, **kwargs):
            nonlocal ssh_probe_attempts
            calls.append((cmd, kwargs))
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                checks = len([call for call, _ in calls if call[:3] == cmd[:3]])
                if checks == 1:
                    return completed(stdout="inactive\n", returncode=3)
                return completed(stdout="active\n")
            if cmd[:5] == [
                "mkosi",
                "--directory",
                str(self.paths.image_dir),
                "--machine",
                app.identity_for().machine_name,
            ] and cmd[-2:] == ["--", "true"]:
                ssh_probe_attempts += 1
                if ssh_probe_attempts == 1:
                    return completed(returncode=255)
                return completed()
            return completed()

        app = AgenticVM(
            self.paths,
            self.cwd,
            runner=runner,
            sleeper=lambda seconds: sleeps.append(seconds),
        )
        result = app.run_vm([])

        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(ssh_probe_attempts, 2)

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

    def test_stop_requests_graceful_shutdown_and_removes_metadata(self):
        calls = []
        is_active_checks = 0

        def runner(cmd, **kwargs):
            nonlocal is_active_checks
            calls.append((cmd, kwargs))
            if cmd[:4] == [
                "systemctl",
                "--user",
                "status",
                app.identity_for().unit_name,
            ]:
                return completed(stdout="Loaded: loaded\n")
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                is_active_checks += 1
                if is_active_checks == 1:
                    return completed(stdout="active\n")
                return completed(stdout="inactive\n", returncode=3)
            if cmd[:3] == ["systemctl", "--user", "is-failed"]:
                return completed(stdout="inactive\n", returncode=1)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner, sleeper=lambda _: None)
        app.ensure_directories()
        app.write_state(app.identity_for())
        app.stop()
        self.assertFalse(app.identity_for().state_file.exists())
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
                "poweroff",
            ],
        )
        self.assertEqual(len(calls), 4)

    def test_stop_forces_after_timeout(self):
        calls = []
        sleeps = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:4] == [
                "systemctl",
                "--user",
                "status",
                app.identity_for().unit_name,
            ]:
                return completed(stdout="Loaded: loaded\n")
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="active\n")
            if cmd[:3] == ["systemctl", "--user", "is-failed"]:
                return completed(stdout="inactive\n", returncode=1)
            return completed()

        app = AgenticVM(
            self.paths,
            self.cwd,
            runner=runner,
            sleeper=lambda seconds: sleeps.append(seconds),
        )
        app.ensure_directories()
        app.write_state(app.identity_for())
        app.stop(timeout_seconds=2.0, poll_interval_seconds=1.0)
        self.assertFalse(app.identity_for().state_file.exists())
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
                "poweroff",
            ],
        )
        self.assertEqual(
            calls[-2][0],
            ["systemctl", "--user", "stop", app.identity_for().unit_name],
        )
        self.assertEqual(
            calls[-1][0],
            ["systemctl", "--user", "is-failed", app.identity_for().unit_name],
        )
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_stop_force_skips_graceful_shutdown(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:4] == [
                "systemctl",
                "--user",
                "status",
                app.identity_for().unit_name,
            ]:
                return completed(stdout="Loaded: loaded\n")
            if cmd[:3] == ["systemctl", "--user", "is-failed"]:
                return completed(stdout="inactive\n", returncode=1)
            return completed()

        app = AgenticVM(self.paths, self.cwd, runner=runner)
        app.ensure_directories()
        app.write_state(app.identity_for())
        app.stop(force=True)
        self.assertEqual(
            calls[1][0], ["systemctl", "--user", "stop", app.identity_for().unit_name]
        )

    def test_stop_resets_failed_unit(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:4] == [
                "systemctl",
                "--user",
                "status",
                app.identity_for().unit_name,
            ]:
                return completed(stdout="Loaded: loaded\n")
            if cmd[:3] == ["systemctl", "--user", "is-active"]:
                return completed(stdout="active\n")
            if cmd[:3] == ["systemctl", "--user", "is-failed"]:
                return completed(stdout="failed\n")
            return completed()

        app = AgenticVM(
            self.paths,
            self.cwd,
            runner=runner,
            sleeper=lambda _: None,
        )
        app.ensure_directories()
        app.write_state(app.identity_for())
        app.stop(timeout_seconds=0.0, poll_interval_seconds=0.0)
        self.assertFalse(app.identity_for().state_file.exists())
        self.assertEqual(
            calls[-2][0],
            ["systemctl", "--user", "is-failed", app.identity_for().unit_name],
        )
        self.assertEqual(
            calls[-1][0],
            ["systemctl", "--user", "reset-failed", app.identity_for().unit_name],
        )

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
        self.assertEqual(
            calls[0][0], ["mkosi", "--directory", str(self.paths.image_dir), "genkey"]
        )
        self.assertEqual(
            calls[1][0],
            ["mkosi", "--directory", str(self.paths.image_dir), "-f", "build"],
        )

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
            app.render_template(self.paths.template_dir / "mkosi.conf.in"),
        )
        self.assertEqual(
            self.paths.root_partition_file.read_text(encoding="utf-8"),
            app.render_template(
                self.paths.template_dir / "mkosi.repart" / "10-root.conf"
            ),
        )

    def test_template_root_partition_uses_btrfs(self):
        app = AgenticVM(self.paths, self.cwd, runner=Mock())
        app.ensure_mkosi_workspace()
        self.assertIn(
            "Format=btrfs",
            self.paths.root_partition_file.read_text(encoding="utf-8"),
        )

    def test_mkosi_config_renders_package_array(self):
        app = AgenticVM(self.paths, self.cwd, runner=Mock())
        rendered = app.render_template(self.paths.template_dir / "mkosi.conf.in")
        self.assertIn(f"Packages={','.join(DEFAULT_PACKAGES)}", rendered)


if __name__ == "__main__":
    unittest.main()
