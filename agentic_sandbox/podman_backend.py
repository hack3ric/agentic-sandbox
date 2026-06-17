from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence, TextIO

from agentic_sandbox.consts import HOST_BIND_MOUNTS, PODMAN_PACKAGES

from .backend import Backend
from .spinner import DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS, Spinner

HOST_PACMAN_MIRRORLIST = Path("/etc/pacman.d/mirrorlist")
DEFAULT_BOOT_TIMEOUT_SECONDS = 30.0
DEFAULT_BOOT_POLL_INTERVAL_SECONDS = 1.0


class PodmanBackend(Backend):
    name = "podman"

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
        self.image_dir = paths.podman_image_dir
        self.template_dir = paths.podman_template_dir
        self.build_marker = paths.podman_build_marker
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
        self.ensure_workspace()
        self.ensure_image_built()
        if not self.is_known(identity):
            self.run(self.create_cmd(identity, cwd))
        if wait and not self.is_running(identity):
            self.run(["podman", "start", identity.machine_name])
            self.wait_for_container(identity)

    def ssh(self, identity, cwd: Path, extra_args: Sequence[str]) -> int:
        if not self.is_running(identity):
            raise self.error_type(f"{identity.unit_name} is not active")
        return self.run(self.exec_cmd(identity, cwd, extra_args)).returncode

    def stop(
        self,
        identity,
        force: bool,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> bool:
        del poll_interval_seconds
        if not self.is_known(identity):
            return False
        if force:
            self.remove_container(identity, force=True)
            return True
        stop_result = self.run(
            [
                "podman",
                "stop",
                "--time",
                str(max(int(timeout_seconds), 0)),
                identity.machine_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.remove_container(identity, force=stop_result.returncode != 0)
        return True

    def rebuild(self) -> None:
        self.ensure_image_dir()
        self.ensure_workspace(force=True)
        self.run(self.build_cmd())
        self.write_build_marker()

    def is_running(self, identity) -> bool:
        result = self.run(
            [
                "podman",
                "container",
                "inspect",
                "--format",
                "{{.State.Running}}",
                identity.machine_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "true"

    def is_known(self, identity) -> bool:
        result = self.run(
            ["podman", "container", "exists", identity.machine_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def create_cmd(self, identity, cwd: Path) -> list[str]:
        return [
            "podman",
            "create",
            "--name",
            identity.machine_name,
            "--hostname",
            identity.machine_name,
            "--workdir",
            str(cwd),
            *self.mount_args(cwd),
            self.image_tag,
            "/bin/sh",
            "-lc",
            'trap "exit 0" TERM INT; while :; do sleep 3600 & wait $! || :; done',
        ]

    def exec_cmd(self, identity, cwd: Path, extra_args: Sequence[str]) -> list[str]:
        forwarded = list(extra_args)
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        command = ["podman", "exec", "--workdir", str(cwd)]
        if not forwarded or self.should_allocate_tty():
            command.append("-it")
        command.append(identity.machine_name)
        if not forwarded:
            command.extend(["/bin/sh", "-lc", f"cd {shlex.quote(str(cwd))} && exec ${{SHELL:-/bin/bash}} -l"])
            return command
        remote_command = " ".join(shlex.quote(arg) for arg in forwarded)
        command.extend(["/bin/sh", "-lc", f"cd {shlex.quote(str(cwd))} && exec {remote_command}"])
        return command

    def mount_args(self, cwd: Path) -> list[str]:
        args = ["--volume", f"{cwd}:{cwd}"]
        for relative in HOST_BIND_MOUNTS:
            source = self.paths.home / relative
            if source.exists():
                args.extend(["--volume", f"{source}:{Path('/root') / relative}"])
        return args

    @property
    def image_tag(self) -> str:
        return "localhost/agentic-sandbox:base"

    def build_cmd(self) -> list[str]:
        return [
            "podman",
            "build",
            "--tag",
            self.image_tag,
            "--file",
            str(self.image_dir / "Containerfile"),
            str(self.image_dir),
        ]

    def ensure_image_dir(self) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def ensure_workspace(self, force: bool = False) -> None:
        for source in sorted(self.template_dir.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(self.template_dir)
            if relative.suffix == ".in":
                relative = relative.with_suffix("")
                content = self.render_template(source)
            else:
                content = source.read_text(encoding="utf-8")
            target = self.image_dir / relative
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

    def render_template(self, source: Path) -> str:
        template = source.read_text(encoding="utf-8")
        return template.replace("@PACKAGES@", " \\\n        ".join(PODMAN_PACKAGES))

    def sync_host_mirrorlist(self, force: bool = False) -> None:
        content = ""
        if HOST_PACMAN_MIRRORLIST.exists():
            content = HOST_PACMAN_MIRRORLIST.read_text(encoding="utf-8")
        target = self.image_dir / "host-mirrorlist"
        if force or not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")

    def ensure_image_built(self) -> None:
        if self.build_marker.exists():
            return
        self.run(self.build_cmd())
        self.write_build_marker()

    def wait_for_container(
        self,
        identity,
        timeout_seconds: float = DEFAULT_BOOT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_BOOT_POLL_INTERVAL_SECONDS,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        spinner = self.make_spinner(f"Waiting for {identity.machine_name} to start")
        while True:
            if not self.is_running(identity):
                spinner.finish()
                raise self.error_type(f"{identity.unit_name} is not active")
            result = self.run(
                ["podman", "exec", identity.machine_name, "true"],
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

    def remove_container(self, identity, force: bool) -> None:
        command = ["podman", "rm"]
        if force:
            command.extend(["-f", "-t", "0"])
        command.append(identity.machine_name)
        self.run(command, check=False, capture_output=True, text=True)

    def write_build_marker(self) -> None:
        payload = {
            "image_dir": str(self.image_dir),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.build_marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def should_allocate_tty(self) -> bool:
        return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

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
