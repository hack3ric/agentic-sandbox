from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence, TextIO

from agentic_vm.consts import DEFAULT_PACKAGES, HOST_BIND_MOUNTS

from .backend import Backend
from .spinner import DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS, Spinner

DEFAULT_RUNTIME_SIZE = "32G"
DEFAULT_BOOT_TIMEOUT_SECONDS = 60.0
DEFAULT_BOOT_POLL_INTERVAL_SECONDS = 1.0
HOST_PACMAN_MIRRORLIST = Path("/etc/pacman.d/mirrorlist")
HOST_MIRRORLIST_TARGETS = (
    Path("mkosi.sandbox/etc/pacman.d/mirrorlist"),
    Path("mkosi.extra/etc/pacman.d/mirrorlist"),
)
GUEST_WORK_MOUNT = Path("/mnt/work")


class MkosiBackend(Backend):
    def __init__(
        self,
        paths,
        runner=None,
        sleeper=None,
        status_stream: TextIO | None = None,
        spinner_enabled: bool | None = None,
        spinner_frame_interval_seconds: float = DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS,
        error_type: type[RuntimeError] = RuntimeError,
    ):
        self.paths = paths
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep
        self.status_stream = status_stream or sys.stderr
        self.spinner_enabled = (
            spinner_enabled
            if spinner_enabled is not None
            else hasattr(self.status_stream, "isatty") and self.status_stream.isatty()
        )
        self.spinner_frame_interval_seconds = spinner_frame_interval_seconds
        self.error_type = error_type

    def create(self, identity, cwd: Path, wait: bool = False) -> None:
        self.ensure_image_dir()
        self.ensure_mkosi_workspace()
        self.ensure_ssh_credentials()
        self.ensure_image_built()
        command = [
            *self.mkosi_cmd(
                "--vmm=qemu",
                "--machine",
                identity.machine_name,
                "--ephemeral=yes",
                "--console=read-only",
                "--runtime-size",
                DEFAULT_RUNTIME_SIZE,
                "--runtime-network=user",
                "--register=no",
                "vm",
            ),
            *self.runtime_tree_args(cwd),
        ]
        self.run(
            [
                "systemd-run",
                "--user",
                "--unit",
                identity.unit_name,
                "--description",
                f"agentic-vm VM for {identity.cwd}",
                "--collect",
                "--service-type=exec",
                "--working-directory",
                str(self.paths.image_dir),
                *command,
            ]
        )
        if wait:
            self.wait_for_machine(identity)

    def ssh(self, identity, cwd: Path, extra_args: Sequence[str]) -> int:
        if not self.is_running(identity):
            raise self.error_type(f"{identity.unit_name} is not active")
        command = self.mkosi_cmd("--machine", identity.machine_name, "ssh")
        command.extend(["--", *self.ssh_remote_args(identity, cwd, extra_args)])
        return self.run(command).returncode

    def stop(
        self,
        identity,
        force: bool,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        unit_exists = self.is_known(identity)
        if not unit_exists:
            return False
        if force:
            self.force_stop_unit(identity)
            return True
        self.request_graceful_stop(identity)
        if not self.wait_for_unit_inactive(
            identity,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ):
            self.force_stop_unit(identity)
        return True

    def rebuild(self) -> None:
        self.ensure_image_dir()
        self.ensure_mkosi_workspace(force=True)
        self.ensure_ssh_credentials()
        self.run(self.mkosi_cmd("-f", "build"))
        self.write_build_marker()

    def is_running(self, identity) -> bool:
        result = self.run(
            ["systemctl", "--user", "is-active", identity.unit_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "active"

    def is_known(self, identity) -> bool:
        result = self.run(
            ["systemctl", "--user", "status", identity.unit_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 or "Loaded:" in (result.stdout or "") + (
            result.stderr or ""
        )

    def runtime_tree_args(self, cwd: Path) -> list[str]:
        args = ["--runtime-tree", f"{cwd}:{GUEST_WORK_MOUNT}"]
        for relative in HOST_BIND_MOUNTS:
            source = self.paths.home / relative
            if source.exists():
                args.extend(["--runtime-tree", f"{source}:{Path('/root') / relative}"])
        return args

    def should_allocate_ssh_tty(self) -> bool:
        return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    def ssh_remote_args(
        self, identity, cwd: Path, extra_args: Sequence[str]
    ) -> list[str]:
        forwarded = list(extra_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        remote_cwd = shlex.quote(str(cwd))
        setup = self.remote_workspace_setup(cwd)
        if not forwarded:
            return ["-t", f"{setup}cd {remote_cwd} && exec ${{SHELL:-/bin/bash}} -l"]
        remote_command = " ".join(shlex.quote(arg) for arg in forwarded)
        if self.should_allocate_ssh_tty():
            return ["-t", f"{setup}cd {remote_cwd} && exec {remote_command}"]
        return [f"{setup}cd {remote_cwd} && exec {remote_command}"]

    def remote_workspace_setup(self, cwd: Path) -> str:
        remote_cwd = shlex.quote(str(cwd))
        guest_work_mount = shlex.quote(str(GUEST_WORK_MOUNT))
        return (
            f"mkdir -p {guest_work_mount} {remote_cwd} && "
            f"if ! mountpoint -q {remote_cwd}; then "
            f"mount --bind {guest_work_mount} {remote_cwd}; "
            f"fi && "
        )

    def ensure_image_dir(self) -> None:
        self.paths.image_dir.mkdir(parents=True, exist_ok=True)

    def ensure_mkosi_workspace(self, force: bool = False) -> None:
        for source in sorted(self.paths.template_dir.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(self.paths.template_dir)
            if relative.suffix == ".in":
                relative = relative.with_suffix("")
                content = self.render_template(source)
            else:
                content = source.read_text(encoding="utf-8")
            target = self.paths.image_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                force
                or not target.exists()
                or target.read_text(encoding="utf-8") != content
            ):
                target.write_text(content, encoding="utf-8")
            source_mode = source.stat().st_mode & 0o777
            if target.stat().st_mode & 0o777 != source_mode:
                target.chmod(source_mode)
        self.sync_host_mirrorlist(force=force)

    def sync_host_mirrorlist(self, force: bool = False) -> None:
        if not HOST_PACMAN_MIRRORLIST.exists():
            # raise self.error_type(
            #     f"host pacman mirrorlist is missing: {HOST_PACMAN_MIRRORLIST}"
            # )
            return
        content = HOST_PACMAN_MIRRORLIST.read_text(encoding="utf-8")
        for relative in HOST_MIRRORLIST_TARGETS:
            target = self.paths.image_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                force
                or not target.exists()
                or target.read_text(encoding="utf-8") != content
            ):
                target.write_text(content, encoding="utf-8")

    def render_template(self, source: Path) -> str:
        template = source.read_text(encoding="utf-8")
        return template.replace("@PACKAGES@", ",".join(DEFAULT_PACKAGES))

    def ensure_ssh_credentials(self) -> None:
        key = self.paths.image_dir / "mkosi.key"
        crt = self.paths.image_dir / "mkosi.crt"
        if key.exists() and crt.exists():
            return
        self.run(["mkosi", "--directory", str(self.paths.image_dir), "genkey"])

    def ensure_image_built(self) -> None:
        if self.paths.build_marker.exists():
            return
        self.run(self.mkosi_cmd("build"))
        self.write_build_marker()

    def wait_for_machine(
        self,
        identity,
        timeout_seconds: float = DEFAULT_BOOT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_BOOT_POLL_INTERVAL_SECONDS,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        spinner = self.make_spinner(f"Waiting for {identity.machine_name} to boot")
        while True:
            if not self.is_running(identity):
                spinner.finish()
                raise self.error_type(f"{identity.unit_name} is not active")
            result = self.run(
                self.mkosi_cmd("--machine", identity.machine_name, "ssh", "--", "true"),
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                spinner.finish()
                return
            if time.monotonic() >= deadline:
                spinner.finish()
                raise self.error_type(
                    f"timed out waiting for {identity.machine_name} to become reachable"
                )
            self.sleeper(poll_interval_seconds)

    def request_graceful_stop(self, identity) -> None:
        self.run(
            self.mkosi_cmd("--machine", identity.machine_name, "ssh", "--", "poweroff"),
            check=False,
            capture_output=True,
            text=True,
        )

    def wait_for_unit_inactive(
        self,
        identity,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        remaining = max(timeout_seconds, 0.0)
        spinner = self.make_spinner(f"Waiting for {identity.machine_name} to power off")
        while True:
            if not self.is_running(identity):
                spinner.finish()
                return True
            if remaining <= 0:
                spinner.finish()
                return False
            sleep_duration = min(poll_interval_seconds, remaining)
            self.sleeper(sleep_duration)
            remaining -= sleep_duration

    def force_stop_unit(self, identity) -> None:
        stop_result = self.run(
            ["systemctl", "--user", "stop", identity.unit_name], check=False
        )
        if stop_result.returncode != 0 or self.is_unit_failed(identity.unit_name):
            self.run(
                ["systemctl", "--user", "reset-failed", identity.unit_name],
                check=False,
            )

    def is_unit_failed(self, unit_name: str) -> bool:
        result = self.run(
            ["systemctl", "--user", "is-failed", unit_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "failed"

    def write_build_marker(self) -> None:
        import json
        from datetime import datetime, timezone

        payload = {
            "image_dir": str(self.paths.image_dir),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.paths.build_marker.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def mkosi_cmd(self, *args: str) -> list[str]:
        return ["mkosi", "--directory", str(self.paths.image_dir), *args]

    def run(self, command: Sequence[str], **kwargs):
        kwargs.setdefault("check", True)
        return self.runner(list(command), **kwargs)

    def make_spinner(self, message: str) -> Spinner:
        spinner = Spinner(
            self.status_stream,
            message,
            self.spinner_frame_interval_seconds,
        )
        if self.spinner_enabled:
            spinner.start()
        return spinner
